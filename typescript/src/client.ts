/**
 * WS-Tunnel 客户端
 *
 * 连接到隧道服务器，接收请求并转发到本地目标服务
 */

import WebSocket from 'ws';
// undici 的 Agent 与 fetch 必须同源使用：Node 内建 fetch + 外部 undici Agent
// 会因跨拷贝 handler 协议不匹配丢失全部响应头（连带破坏 SSE 判定与
// chunked_http 的 Content-Type/Content-Length 判定）
import { Agent, fetch as undiciFetch } from 'undici';
import * as net from 'net';
import {
  AuthMessage,
  AuthOkMessage,
  AuthErrorMessage,
  TunnelRequest,
  TunnelResponse,
  PingMessage,
  MessageType,
  createAuthMessage,
  createPongMessage,
  createResponse,
  parseMessage,
  StreamStartMessage,
  StreamChunkMessage,
  StreamEndMessage,
  TcpConnectMessage,
  TcpDataMessage,
  TcpCloseMessage,
} from './protocol.js';
import { decodeTcpDataFrame, encodeTcpDataFrame } from './framing.js';

export interface TunnelClientConfig {
  /** 服务端 WebSocket URL */
  serverUrl: string;
  /** 隧道令牌 */
  token: string;
  /** 本地目标服务 URL */
  targetUrl: string;
  /** 重连间隔（毫秒） */
  reconnectInterval?: number;
  /** 最大重连次数（0 表示无限） */
  maxReconnectAttempts?: number;
  /** 请求超时（毫秒，默认 300000 = 5 分钟） */
  requestTimeout?: number;
  /** 是否强制抢占已有连接 */
  force?: boolean;
  /** keepalive：周期性发送协议 ping（毫秒，默认 25000） */
  keepaliveInterval?: number;
  /** keepalive：超过该时长未收到 pong 判定连接死亡并重连（毫秒，默认 45000） */
  keepaliveTimeout?: number;
  /** 普通响应体内存上限（字节，默认 104857600 = 100MB；与生产服务端 cap 对齐）。0 = 不限制，超限返回 502 并中止读取 */
  maxResponseBytes?: number;
  /**
   * 非 SSE 响应超过该字节数、且协商了 chunked_http、且服务端放行
   * （request.stream_ok）时切换流式回传（协议 v2 T3）。默认 1048576 = 1MB；
   * 0 = 从不流式。env: TUNELY_STREAM_THRESHOLD_BYTES
   */
  streamThresholdBytes?: number;
  /** 多隧道模式下的会话标签（单隧道不设）；用于连接期日志前缀 */
  name?: string;
}

// 共享 undici Agent（0.7.3：连接池跨请求复用，此前每请求新建+关闭，TLS 握手无法复用）。
// headers/bodyTimeout 设为极大值兜底（防 undici 默认 300s 抢跑），每请求的真实超时
// 由 AbortController 执行——语义与旧 per-request Agent 一致
let sharedDispatcher: Agent | null = null;

function getSharedDispatcher(): Agent {
  if (!sharedDispatcher) {
    sharedDispatcher = new Agent({
      headersTimeout: 2 ** 31 - 1,
      bodyTimeout: 2 ** 31 - 1,
      connectTimeout: 30_000,
    });
  }
  return sharedDispatcher;
}

export interface TunnelClientEvents {
  onConnect?: (domain: string) => void;
  onDisconnect?: () => void;
  onRequest?: (request: TunnelRequest) => void;
  onError?: (error: Error) => void;
}

/**
 * 归一化服务端下发的请求路径：确保以 "/" 开头。
 *
 * 防止 "@evil/" 这类不以 "/" 开头的 path 在 URL 拼接时改写 authority
 * （如 `http://127.0.0.1:3080` + `@evil/` → 请求打到 evil 主机，SSRF）。
 */
export function normalizePath(path: string): string {
  return path.startsWith('/') ? path : `/${path}`;
}

/**
 * WS 发送背压阈值：出站缓冲超过该值时暂停发送，等待对端消化。
 * 防止上行慢于本地产生数据时，ws 库内部缓冲无界增长。
 */
const WS_MAX_BUFFERED_AMOUNT = 4 * 1024 * 1024;

/** 单个本地 TCP 连接的状态（TCP 模式） */
interface TcpConnState {
  socket: net.Socket;
  /** 发往服务端的下一个数据包序号 */
  sequence: number;
  /** 是否已发送过 tcp_close（保证幂等） */
  closeSent: boolean;
  /** 发往服务端的发送串行链：背压等待时保持 tcp_data / tcp_close 顺序 */
  sendChain: Promise<void>;
  /** 写入本地目标的串行链：socket 缓冲满时等 drain，保持写入顺序 */
  writeChain: Promise<void>;
}

export class TunnelClient {
  private config: Required<TunnelClientConfig>;
  private ws: WebSocket | null = null;
  private running = false;
  private connected = false;
  private domain: string | null = null;
  // 协议 v2 能力协商结果（AuthOk.capabilities 的本连接快照；
  // 重连重新认证后刷新，断开时清空——旧连接的能力不带入新连接）
  private negotiated: Set<string> = new Set();
  private reconnectCount = 0;
  private consecutiveRejectCount = 0;
  private wasConnectedBefore = false;
  private events: TunnelClientEvents = {};

  // TCP 模式：conn_id -> 本地连接状态
  private tcpConnections: Map<string, TcpConnState> = new Map();
  // TCP 模式：目标服务地址（从 targetUrl 解析）
  private targetHost = 'localhost';
  private targetPort = 8080;

  constructor(config: TunnelClientConfig) {
    this.config = {
      serverUrl: config.serverUrl,
      token: config.token,
      targetUrl: config.targetUrl,
      reconnectInterval: config.reconnectInterval ?? 5000,
      maxReconnectAttempts: config.maxReconnectAttempts ?? 0,
      requestTimeout: config.requestTimeout ?? 300000,
      maxResponseBytes: config.maxResponseBytes ?? 104857600,
      streamThresholdBytes: config.streamThresholdBytes ?? 1048576,
      force: config.force ?? false,
      keepaliveInterval: config.keepaliveInterval ?? 25000,
      keepaliveTimeout: config.keepaliveTimeout ?? 45000,
      name: config.name ?? '',
    };
    this.parseTargetUrl();
  }

  /** 多隧道日志前缀；单隧道为空串（日志与 0.3.x 逐字节一致） */
  private get logPrefix(): string {
    return this.config.name ? `[${this.config.name}] ` : '';
  }

  /** 是否已连接 */
  get isConnected(): boolean {
    return this.connected;
  }

  /** 分配的域名 */
  get tunnelDomain(): string | null {
    return this.domain;
  }

  /** 设置事件回调 */
  on<K extends keyof TunnelClientEvents>(
    event: K,
    callback: TunnelClientEvents[K]
  ): void {
    this.events[event] = callback;
  }

  /** 启动客户端 */
  async run(): Promise<void> {
    this.running = true;

    while (this.running) {
      try {
        await this.connectAndRun();
        if (!this.running) break;
        this.notifyDisconnect();
        const reconnectDelay = this.config.reconnectInterval;
        console.warn(
          `${this.logPrefix}连接已关闭，${(reconnectDelay / 1000).toFixed(1)}秒后重连`
        );
        await this.sleep(reconnectDelay);
        continue;
      } catch (error) {
        if (!this.running) break;

        this.notifyDisconnect();

        this.reconnectCount++;
        const maxAttempts = this.config.maxReconnectAttempts;

        if (maxAttempts > 0 && this.reconnectCount > maxAttempts) {
          console.error(`${this.logPrefix}超过最大重连次数 (${maxAttempts})，停止`);
          break;
        }

        // Exponential backoff: base * 2^(attempts-1), capped at maxDelay
        const baseInterval = this.config.reconnectInterval;
        const errorMsg = String(error);
        const isRejected = errorMsg.includes('already connected') || errorMsg.includes('已有活跃连接');
        
        if (isRejected) {
          this.consecutiveRejectCount++;
        } else {
          this.consecutiveRejectCount = 0;
        }

        const backoffFactor = Math.min(this.reconnectCount + this.consecutiveRejectCount, 8);
        const maxDelay = 300000; // 5 minutes max
        const delay = Math.min(baseInterval * Math.pow(2, backoffFactor - 1), maxDelay);
        // Add jitter (±20%) to prevent thundering herd
        const jitter = delay * (0.8 + Math.random() * 0.4);
        const actualDelay = Math.round(jitter);

        console.warn(
          `${this.logPrefix}连接断开: ${error}，${(actualDelay / 1000).toFixed(1)}秒后重连 ` +
            `(第 ${this.reconnectCount} 次, backoff=${backoffFactor})`
        );
        await this.sleep(actualDelay);
      }
    }
  }

  /** 停止客户端 */
  stop(): void {
    this.running = false;
    this.cleanupTcpConnections();
    if (this.ws) {
      this.ws.close();
    }
  }

  /**
   * 解析目标 URL，提取主机和端口（TCP 模式使用）
   */
  private parseTargetUrl(): void {
    try {
      const url = new URL(this.config.targetUrl);
      this.targetHost = url.hostname || 'localhost';
      this.targetPort = url.port
        ? parseInt(url.port, 10)
        : url.protocol === 'https:'
          ? 443
          : 80;
    } catch {
      console.warn(`解析目标 URL 失败: ${this.config.targetUrl}，使用默认值 localhost:8080`);
      this.targetHost = 'localhost';
      this.targetPort = 8080;
    }
  }

  private async connectAndRun(): Promise<void> {
    console.log(`正在连接到 ${this.config.serverUrl}...`);

    return new Promise((resolve, reject) => {
      // 显式启用 permessage-deflate（ws 客户端默认关闭），
      // 服务端（uvicorn/websockets）默认开启，双方协商后压缩 wire 流量
      const ws = new WebSocket(this.config.serverUrl, { perMessageDeflate: true });
      this.ws = ws;

      // keepalive：空闲长连接会被中间设备静默丢弃，必须主动探测。
      // 周期发协议 ping；超过 keepaliveTimeout 无 pong 时强断触发重连。
      let lastPong = Date.now();
      const keepaliveTimer = setInterval(() => {
        if (Date.now() - lastPong > this.config.keepaliveTimeout) {
          console.warn('keepalive 超时：连接已静默死亡，重连');
          ws.terminate();
          return;
        }
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: MessageType.PING }));
        }
      }, this.config.keepaliveInterval);

      ws.on('open', () => {
        const useForce = this.config.force || (this.wasConnectedBefore && this.consecutiveRejectCount > 0);
        const authMessage = createAuthMessage(this.config.token, useForce);
        ws.send(JSON.stringify(authMessage));
      });

      // ws 库回调第三参 isBinary 区分文本（JSON 控制面/数据面）/二进制
      // （binary_frames 数据面帧）。mock 测试只 emit 单参（isBinary=undefined
      // → 按 text 处理），与既有测试兼容。
      ws.on('message', async (data: Buffer, isBinary?: boolean) => {
        try {
          // 协议 v2 binary_frames：二进制消息 = tcp_data 帧（仅协商连接处理）
          if (isBinary) {
            this.handleBinaryMessage(data);
            return;
          }
          const message = parseMessage(data.toString());

          switch (message.type) {
            case MessageType.AUTH_OK:
              this.handleAuthOk(message as AuthOkMessage);
              break;

            case MessageType.AUTH_ERROR:
              this.handleAuthError(message as AuthErrorMessage);
              ws.close();
              reject(new Error((message as AuthErrorMessage).error));
              break;

            case MessageType.PING:
              await this.sendToWs(ws, JSON.stringify(createPongMessage()));
              break;

            case MessageType.PONG:
              lastPong = Date.now();
              break;

            case MessageType.REQUEST:
              await this.handleRequest(message as TunnelRequest, ws);
              break;

            case MessageType.TCP_CONNECT:
              this.handleTcpConnect(message as TcpConnectMessage, ws);
              break;

            case MessageType.TCP_DATA:
              this.handleTcpData(message as TcpDataMessage);
              break;

            case MessageType.TCP_CLOSE:
              this.handleTcpClose(message as TcpCloseMessage);
              break;

            default:
              console.warn(`未知消息类型: ${message.type}`);
          }
        } catch (error) {
          console.error('处理消息错误:', error);
          this.events.onError?.(error as Error);
        }
      });

      ws.on('close', () => {
        clearInterval(keepaliveTimer);
        // WebSocket 断开后服务端会重新分配 conn_id，旧本地连接全部丢弃，防止泄漏
        this.cleanupTcpConnections();
        // 协商结果属于单条连接：断开后清空，重连重新认证协商
        this.negotiated = new Set();
        // 无论是正常关闭还是异常关闭，只要之前是已认证状态，都需要触发 onDisconnect
        // 否则调用方（如 tunnelService）的状态会停留在 connected=true，导致 UI 显示误连接。
        // F12：close handler 与 run 循环两条路径都会到达 notifyDisconnect，
        // 由 connected 标志去重，保证每次已认证连接断开恰好触发一次。
        this.notifyDisconnect();
        this.domain = null;
        resolve();
      });

      ws.on('error', (error) => {
        console.error('WebSocket 错误:', error);
        this.events.onError?.(error);
        reject(error);
      });
    });
  }

  private handleAuthOk(message: AuthOkMessage): void {
    this.domain = message.domain;
    // 协议 v2：协商结果 = AuthOk.capabilities（缺字段 = 空集合，旧服务端
    // 不带该字段时全 JSON，行为与 0.7.x 一致）
    this.negotiated = new Set(message.capabilities ?? []);
    this.connected = true;
    this.wasConnectedBefore = true;
    this.reconnectCount = 0;
    this.consecutiveRejectCount = 0;
    console.log(`已连接: domain=${this.domain}`);
    this.events.onConnect?.(this.domain);
  }

  /**
   * 断开通知（恰好一次，F12）：仅当从「已认证」状态跌落时触发。
   * close handler 与 run 循环（成功/异常分支）都会到达这里，
   * 用 connected 标志去重，保证每次已认证连接断开恰好回调一次 onDisconnect。
   */
  private notifyDisconnect(): void {
    if (this.connected) {
      this.connected = false;
      this.events.onDisconnect?.();
    }
  }

  private handleAuthError(message: AuthErrorMessage): void {
    console.error(`认证失败: ${message.error}`);
  }

  /**
   * 检查是否是 SSE 响应
   */
  private isSSEResponse(headers: Headers): boolean {
    const contentType = headers.get('content-type') || '';
    return contentType.toLowerCase().includes('text/event-stream');
  }

  /**
   * 带内存上限读取响应体（0.7.3）：超过 cap 立即中止读取并返回 tooLarge，
   * 避免"先全量缓冲再判断"的 OOM 窗口。解码语义与 text() 一致（UTF-8 容错替换）。
   */
  private async readBodyCapped(
    body: ReadableStream<Uint8Array>,
    cap: number
  ): Promise<{ text: string; tooLarge: boolean }> {
    const reader = body.getReader();
    const parts: Uint8Array[] = [];
    let total = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > cap) {
          reader.cancel().catch(() => {});
          return { text: '', tooLarge: true };
        }
        parts.push(value);
      }
    } catch (e) {
      reader.cancel().catch(() => {});
      throw e;
    }
    const merged = new Uint8Array(total);
    let offset = 0;
    for (const part of parts) {
      merged.set(part, offset);
      offset += part.byteLength;
    }
    return { text: new TextDecoder('utf-8', { fatal: false }).decode(merged), tooLarge: false };
  }

  private async handleRequest(
    request: TunnelRequest,
    ws: WebSocket
  ): Promise<void> {
    this.events.onRequest?.(request);

    const startTime = Date.now();

    try {
      // 构建完整 URL（path 先归一化，防止 "@evil/" 改写 authority）
      const url = `${this.config.targetUrl.replace(/\/$/, '')}${normalizePath(request.path)}`;

      // 解析请求体
      let body: string | undefined;
      if (request.body) {
        body = request.body;
      }

      // 清理转发的请求头，移除可能导致 fetch 失败的头部
      const cleanHeaders: Record<string, string> = {};
      if (request.headers) {
        const skipHeaders = new Set([
          'host', 'connection', 'keep-alive', 'transfer-encoding',
          'te', 'trailer', 'upgrade', 'proxy-authorization',
          'proxy-connection',
        ]);
        for (const [key, value] of Object.entries(request.headers)) {
          if (!skipHeaders.has(key.toLowerCase())) {
            cleanHeaders[key] = value;
          }
        }
      }

      // request.timeout 单位是秒，this.config.requestTimeout 单位是毫秒
      const timeoutSeconds = request.timeout ?? (this.config.requestTimeout / 1000);
      const timeoutMs = timeoutSeconds * 1000;

      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

      try {
        // undici 同源 fetch：dispatcher 选项类型安全，且响应头可见（见文件头注释）
        const fetchResponse = await undiciFetch(url, {
          method: request.method,
          headers: cleanHeaders,
          body: body,
          signal: controller.signal,
          dispatcher: getSharedDispatcher(),
        });

        const responseHeaders = Object.fromEntries(fetchResponse.headers.entries());

        // 检查是否是 SSE 响应
        if (this.isSSEResponse(fetchResponse.headers)) {
          // SSE 流式响应处理
          await this.handleSSEResponse(
            request.id,
            fetchResponse.status,
            responseHeaders,
            fetchResponse.body!,
            ws,
            startTime
          );
          return; // SSE 响应已通过流式消息发送
        }

        // 协议 v2 chunked_http（T3）门控：stream_ok 请求 + 已协商 + 阈值 > 0，
        // 三者齐备才可能对非 SSE 响应切流式；其余情况既有缓冲路径行为不变
        // （wire 键为 snake_case 的 stream_ok，同 tunnel_id 等字段先例）
        const threshold = this.config.streamThresholdBytes;
        const canStream =
          request.stream_ok === true &&
          this.negotiated.has('chunked_http') &&
          threshold > 0 &&
          fetchResponse.body != null;

        if (canStream) {
          const contentType = (
            fetchResponse.headers.get('content-type') || ''
          ).toLowerCase();
          const isText =
            contentType.startsWith('text/') ||
            contentType.startsWith('application/json');
          const contentLengthRaw = fetchResponse.headers.get('content-length');
          const declaredLength =
            contentLengthRaw !== null && /^\d+$/.test(contentLengthRaw)
              ? parseInt(contentLengthRaw, 10)
              : null;

          if (declaredLength !== null && declaredLength > threshold) {
            // Content-Length 已知且超阈值：直接走流式回传
            await this.streamLargeResponse(
              request.id,
              fetchResponse.status,
              responseHeaders,
              fetchResponse.body!.getReader(),
              ws,
              startTime,
              isText
            );
            return;
          }

          if (declaredLength === null) {
            // Content-Length 未知：边缓冲边观察，越过阈值就地切换
            const outcome = await this.bufferOrStreamBody(
              fetchResponse.body!.getReader(),
              threshold,
              this.config.maxResponseBytes ?? 104857600
            );
            if (outcome.mode === 'stream') {
              await this.streamLargeResponse(
                request.id,
                fetchResponse.status,
                responseHeaders,
                outcome.reader,
                ws,
                startTime,
                isText,
                outcome.parts
              );
              return;
            }
            if (outcome.mode === 'tooLarge') {
              const cap = this.config.maxResponseBytes ?? 104857600;
              const response = createResponse(
                request.id,
                502,
                null,
                {},
                `Response too large (> ${cap} bytes, client cap)`,
                Date.now() - startTime
              );
              await this.sendToWs(ws, JSON.stringify(response));
              return;
            }
            // 全程未超阈值：照旧回 TunnelResponse（行为与缓冲路径一致）
            const response = createResponse(
              request.id,
              fetchResponse.status,
              outcome.text,
              responseHeaders,
              undefined,
              Date.now() - startTime
            );
            await this.sendToWs(ws, JSON.stringify(response));
            return;
          }
        }

        // 普通响应：带内存上限读取（0.7.3，超限 502 并中止；对齐生产服务端 cap）
        const cap = this.config.maxResponseBytes ?? 104857600;
        let responseBody: string;
        if (cap > 0 && fetchResponse.body) {
          const capped = await this.readBodyCapped(fetchResponse.body, cap);
          if (capped.tooLarge) {
            const response = createResponse(
              request.id,
              502,
              null,
              {},
              `Response too large (> ${cap} bytes, client cap)`,
              Date.now() - startTime
            );
            await this.sendToWs(ws, JSON.stringify(response));
            return;
          }
          responseBody = capped.text;
        } else {
          responseBody = await fetchResponse.text();
        }
        const durationMs = Date.now() - startTime;

        const response = createResponse(
          request.id,
          fetchResponse.status,
          responseBody,
          responseHeaders,
          undefined,
          durationMs
        );
        await this.sendToWs(ws, JSON.stringify(response));
      } finally {
        clearTimeout(timeoutId);
      }
    } catch (error: any) {
      const durationMs = Date.now() - startTime;
      let response: TunnelResponse;

      if (error.name === 'AbortError') {
        response = createResponse(
          request.id,
          504,
          null,
          {},
          'Target service timeout',
          durationMs
        );
      } else if (error.code === 'ECONNREFUSED') {
        response = createResponse(
          request.id,
          503,
          null,
          {},
          `Target service unavailable: ${error.message}`,
          durationMs
        );
      } else {
        const errorDetail = error.cause
          ? `${error.message} (cause: ${error.cause?.message || error.cause})`
          : error.message;
        console.error(`[Tunnel] Fetch error for ${request.method} ${request.path}: ${errorDetail}`);
        response = createResponse(
          request.id,
          500,
          null,
          {},
          errorDetail,
          durationMs
        );
      }

      await this.sendToWs(ws, JSON.stringify(response));
    }
  }

  /**
   * 处理 SSE 流式响应
   */
  private async handleSSEResponse(
    requestId: string,
    status: number,
    headers: Record<string, string>,
    body: ReadableStream<Uint8Array>,
    ws: WebSocket,
    startTime: number
  ): Promise<void> {
    // 发送 StreamStart
    const startMsg: StreamStartMessage = {
      type: MessageType.STREAM_START,
      id: requestId,
      status,
      headers,
      timestamp: new Date().toISOString(),
    };
    await this.sendToWs(ws, JSON.stringify(startMsg));

    let chunkCount = 0;
    let errorMsg: string | undefined;
    const decoder = new TextDecoder();

    try {
      const reader = body.getReader();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        if (chunk) {
          const chunkMsg: StreamChunkMessage = {
            type: MessageType.STREAM_CHUNK,
            id: requestId,
            data: chunk,
            sequence: chunkCount,
            timestamp: new Date().toISOString(),
          };
          await this.sendToWs(ws, JSON.stringify(chunkMsg));
          chunkCount++;
        }
      }
    } catch (error: any) {
      errorMsg = error.message;
      console.error('SSE 流读取错误:', error);
    }

    // 发送 StreamEnd
    const durationMs = Date.now() - startTime;
    const endMsg: StreamEndMessage = {
      type: MessageType.STREAM_END,
      id: requestId,
      error: errorMsg,
      duration_ms: durationMs,
      total_chunks: chunkCount,
      timestamp: new Date().toISOString(),
    };
    await this.sendToWs(ws, JSON.stringify(endMsg));
  }

  // ============== chunked_http：非 SSE 大响应流式回传（协议 v2 T3） ==============

  /**
   * Content-Length 未知时的缓冲观察读（chunked_http 门控专用）：
   * 累积读取，越过 threshold 立即返回 midStream 结果（已缓冲 parts 含越线块
   * + 未读完的 reader，供就地切换流式）；全程未超阈值返回 text（cap 语义与
   * readBodyCapped 一致：超限中止读取并返回 tooLarge）。
   */
  private async bufferOrStreamBody(
    reader: ReadableStreamDefaultReader<Uint8Array>,
    threshold: number,
    cap: number
  ): Promise<
    | { mode: 'text'; text: string }
    | { mode: 'tooLarge' }
    | {
        mode: 'stream';
        parts: Uint8Array[];
        reader: ReadableStreamDefaultReader<Uint8Array>;
      }
  > {
    const parts: Uint8Array[] = [];
    let total = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.byteLength;
        parts.push(value);
        if (total > threshold) {
          return { mode: 'stream', parts, reader };
        }
        if (cap > 0 && total > cap) {
          reader.cancel().catch(() => {});
          return { mode: 'tooLarge' };
        }
      }
    } catch (e) {
      reader.cancel().catch(() => {});
      throw e;
    }
    const merged = new Uint8Array(total);
    let offset = 0;
    for (const part of parts) {
      merged.set(part, offset);
      offset += part.byteLength;
    }
    return {
      mode: 'text',
      text: new TextDecoder('utf-8', { fatal: false }).decode(merged),
    };
  }

  /**
   * 非 SSE 大响应流式回传（协议 v2 chunked_http，T3）
   *
   * 发送 StreamStart → StreamChunk* → StreamEnd。块编码按 Content-Type：
   * text/* 与 application/json 走 UTF-8 明文（plain，解码器跨块保持状态），
   * 其余走 base64（+33% 局限见 PROTOCOL_V2 §4）。buffered 非空时
   * （Content-Length 未知的就地切换）其拼接内容作为首批数据块，
   * 随后继续消费 reader 剩余部分。
   */
  private async streamLargeResponse(
    requestId: string,
    status: number,
    headers: Record<string, string>,
    reader: ReadableStreamDefaultReader<Uint8Array>,
    ws: WebSocket,
    startTime: number,
    isText: boolean,
    buffered?: Uint8Array[]
  ): Promise<void> {
    const startMsg: StreamStartMessage = {
      type: MessageType.STREAM_START,
      id: requestId,
      status,
      headers,
      timestamp: new Date().toISOString(),
    };
    await this.sendToWs(ws, JSON.stringify(startMsg));

    let chunkCount = 0;
    let errorMsg: string | undefined;
    const decoder = new TextDecoder('utf-8', { fatal: false });

    const sendChunk = async (value: Uint8Array): Promise<void> => {
      if (!value || value.byteLength === 0) return;
      const data = isText
        ? decoder.decode(value, { stream: true })
        : Buffer.from(value).toString('base64');
      const chunkMsg: StreamChunkMessage = {
        type: MessageType.STREAM_CHUNK,
        id: requestId,
        data,
        sequence: chunkCount,
        encoding: isText ? 'plain' : 'base64',
        timestamp: new Date().toISOString(),
      };
      await this.sendToWs(ws, JSON.stringify(chunkMsg));
      chunkCount++;
    };

    try {
      if (buffered && buffered.length > 0) {
        // 就地切换：越过阈值前已缓冲的字节作为首批数据块
        const merged = new Uint8Array(
          buffered.reduce((n, p) => n + p.byteLength, 0)
        );
        let offset = 0;
        for (const part of buffered) {
          merged.set(part, offset);
          offset += part.byteLength;
        }
        await sendChunk(merged);
      }
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) await sendChunk(value);
      }
    } catch (error: any) {
      errorMsg = error.message;
      console.error('大响应流式回传读取错误:', error);
    }

    const endMsg: StreamEndMessage = {
      type: MessageType.STREAM_END,
      id: requestId,
      error: errorMsg,
      duration_ms: Date.now() - startTime,
      total_chunks: chunkCount,
      timestamp: new Date().toISOString(),
    };
    await this.sendToWs(ws, JSON.stringify(endMsg));
  }

  // ============== TCP 模式处理方法 ==============

  /**
   * 处理 TCP 连接建立请求：建立到本地目标服务的连接
   */
  private handleTcpConnect(message: TcpConnectMessage, ws: WebSocket): void {
    const connId = message.conn_id;

    if (this.tcpConnections.has(connId)) {
      console.warn(`TCP 连接已存在，忽略重复的 tcp_connect: ${connId}`);
      return;
    }

    const state: TcpConnState = {
      socket: net.connect({ host: this.targetHost, port: this.targetPort }),
      sequence: 0,
      closeSent: false,
      sendChain: Promise.resolve(),
      writeChain: Promise.resolve(),
    };
    this.tcpConnections.set(connId, state);

    state.socket.on('data', (data: Buffer) => {
      // 协议 v2 binary_frames：协商了就组二进制帧直发（去 base64+JSON 开销）；
      // 否则走 0.7.x 的 JSON+base64 路径（wire 不变）。
      if (this.negotiated.has('binary_frames')) {
        const frame = encodeTcpDataFrame(connId, data);
        // 经串行链发送：WS 背压等待时不乱序，也保证 tcp_close 不会插队到 tcp_data 前
        state.sendChain = state.sendChain
          .then(() => this.sendToWs(ws, frame))
          .catch(() => {});
        return;
      }
      const msg: TcpDataMessage = {
        type: MessageType.TCP_DATA,
        conn_id: connId,
        data: data.toString('base64'),
        sequence: state.sequence++,
        timestamp: new Date().toISOString(),
      };
      // 经串行链发送：WS 背压等待时不乱序，也保证 tcp_close 不会插队到 tcp_data 前
      state.sendChain = state.sendChain
        .then(() => this.sendToWs(ws, JSON.stringify(msg)))
        .catch(() => {});
    });

    // 连接失败（如目标端口拒绝）：向服务端回执带 error 的 tcp_close，由其关闭外部连接
    state.socket.on('error', (error: Error) => {
      console.error(
        `TCP 连接错误: ${connId} -> ${this.targetHost}:${this.targetPort}, ${error.message}`
      );
      this.sendTcpClose(ws, connId, state, error.message);
    });

    // 默认 allowHalfOpen=false：'end'（对端 FIN）后本端也会结束，最终都以 'close' 收场。
    // 'error' 之后也必然触发 'close'，配合 closeSent 幂等标记保证只回执一个 tcp_close。
    state.socket.on('close', () => {
      this.sendTcpClose(ws, connId, state);
      this.tcpConnections.delete(connId);
    });
  }

  /**
   * 处理来自服务端的 TCP 数据（JSON 形态，base64）：解码后与 binary 帧路径
   * 共用 routeTcpPayload 落地。
   */
  private handleTcpData(message: TcpDataMessage): void {
    const data = Buffer.from(message.data, 'base64');
    this.routeTcpPayload(message.conn_id, data);
  }

  /**
   * tcp_data 落地写入本地连接（JSON 与 binary 帧两路共用）
   *
   * 写入走串行链：socket.write() 返回 false（内核缓冲满）时等一次 'drain' 再写下一批，
   * 防止对慢速目标无界缓冲，同时保持写入顺序。
   */
  private routeTcpPayload(connId: string, data: Buffer): void {
    const state = this.tcpConnections.get(connId);
    if (!state) {
      console.warn(`收到未知 TCP 连接的数据: ${connId}`);
      return;
    }

    state.writeChain = state.writeChain
      .then(async () => {
        if (!state.socket.write(data)) {
          await this.awaitSocketDrain(state.socket);
        }
      })
      .catch(() => {});
  }

  /**
   * WS 二进制消息分派（协议 v2 binary_frames，能力门控）：
   * 未协商收到 binary → 丢弃 + warning；畸形帧 → 丢弃 + warning；
   * 解出 conn_id + payload 后与 JSON tcp_data 路径汇合同一落地函数。
   */
  private handleBinaryMessage(data: Buffer): void {
    if (!this.negotiated.has('binary_frames')) {
      console.warn('未协商 binary_frames，收到 WS 二进制消息已丢弃');
      return;
    }
    let connId: string;
    let payload: Uint8Array;
    try {
      ({ connId, payload } = decodeTcpDataFrame(data));
    } catch (error) {
      console.warn(`丢弃畸形 binary 帧: ${error}`);
      return;
    }
    this.routeTcpPayload(connId, Buffer.from(payload));
  }

  /**
   * 处理服务端发起的 TCP 关闭：销毁本地连接，不回执 tcp_close
   */
  private handleTcpClose(message: TcpCloseMessage): void {
    const state = this.tcpConnections.get(message.conn_id);
    if (!state) {
      console.warn(`尝试关闭未知 TCP 连接: ${message.conn_id}`);
      return;
    }

    this.tcpConnections.delete(message.conn_id);
    state.closeSent = true;
    state.socket.destroy();
  }

  /**
   * 向服务端回执 tcp_close（幂等：每条连接最多发送一次）
   */
  private sendTcpClose(
    ws: WebSocket,
    connId: string,
    state: TcpConnState,
    error?: string
  ): void {
    if (state.closeSent) return;
    state.closeSent = true;

    const msg: TcpCloseMessage = {
      type: MessageType.TCP_CLOSE,
      conn_id: connId,
      error: error ?? null,
      timestamp: new Date().toISOString(),
    };
    // 经串行链发送：排在未发完的 tcp_data 之后，且幂等标记同步生效
    state.sendChain = state.sendChain
      .then(() => this.sendToWs(ws, JSON.stringify(msg)))
      .catch(() => {});
  }

  /**
   * 丢弃全部本地 TCP 连接（WebSocket 断开或客户端停止时调用）
   *
   * 服务端重连后会分配新的 conn_id，旧连接无法恢复；
   * WS 已断开，回执无从发送，直接静默销毁防止 socket 泄漏。
   */
  private cleanupTcpConnections(): void {
    for (const state of this.tcpConnections.values()) {
      state.closeSent = true;
      state.socket.destroy();
    }
    this.tcpConnections.clear();
  }

  /**
   * WS 发送背压（F: 发送端无界缓冲）：出站缓冲超过 WS_MAX_BUFFERED_AMOUNT 时
   * 循环等待回落后再发送；连接不再 OPEN 时放弃发送（由 close 路径统一收尾）。
   * data 为 string（JSON 文本）或 Buffer（binary_frames 二进制帧）。
   */
  private async sendToWs(ws: WebSocket, data: string | Buffer): Promise<void> {
    if (!(await this.awaitWsWritable(ws))) {
      return;
    }
    ws.send(data);
  }

  /** 等待 WS 出站缓冲回落；连接离开 OPEN 状态时返回 false（应放弃发送） */
  private async awaitWsWritable(ws: WebSocket): Promise<boolean> {
    while (
      ws.readyState === WebSocket.OPEN &&
      ws.bufferedAmount > WS_MAX_BUFFERED_AMOUNT
    ) {
      await this.sleep(10);
    }
    return ws.readyState === WebSocket.OPEN;
  }

  /**
   * 等待本地 socket 缓冲排空（'drain'）；连接关闭/出错时直接返回，放弃等待。
   */
  private awaitSocketDrain(socket: net.Socket): Promise<void> {
    return new Promise<void>((resolve) => {
      const cleanup = () => {
        socket.off('drain', onDrain);
        socket.off('close', onDone);
        socket.off('error', onDone);
        resolve();
      };
      const onDrain = () => cleanup();
      const onDone = () => cleanup();
      socket.once('drain', onDrain);
      socket.once('close', onDone);
      socket.once('error', onDone);
    });
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}

/** 运行隧道客户端的便捷函数 */
export async function runTunnelClient(
  serverUrl: string,
  token: string,
  targetUrl: string,
  reconnectInterval = 5000
): Promise<void> {
  const client = new TunnelClient({
    serverUrl,
    token,
    targetUrl,
    reconnectInterval,
  });
  await client.run();
}
