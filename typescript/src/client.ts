/**
 * WS-Tunnel 客户端
 *
 * 连接到隧道服务器，接收请求并转发到本地目标服务
 */

import WebSocket from 'ws';
import { Agent } from 'undici';
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
}

export interface TunnelClientEvents {
  onConnect?: (domain: string) => void;
  onDisconnect?: () => void;
  onRequest?: (request: TunnelRequest) => void;
  onError?: (error: Error) => void;
}

/** 单个本地 TCP 连接的状态（TCP 模式） */
interface TcpConnState {
  socket: net.Socket;
  /** 发往服务端的下一个数据包序号 */
  sequence: number;
  /** 是否已发送过 tcp_close（保证幂等） */
  closeSent: boolean;
}

export class TunnelClient {
  private config: Required<TunnelClientConfig>;
  private ws: WebSocket | null = null;
  private running = false;
  private connected = false;
  private domain: string | null = null;
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
      force: config.force ?? false,
    };
    this.parseTargetUrl();
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
        this.connected = false;
        this.events.onDisconnect?.();
        const reconnectDelay = this.config.reconnectInterval;
        console.warn(`连接已关闭，${(reconnectDelay / 1000).toFixed(1)}秒后重连`);
        await this.sleep(reconnectDelay);
        continue;
      } catch (error) {
        if (!this.running) break;

        this.connected = false;
        this.events.onDisconnect?.();

        this.reconnectCount++;
        const maxAttempts = this.config.maxReconnectAttempts;

        if (maxAttempts > 0 && this.reconnectCount > maxAttempts) {
          console.error(`超过最大重连次数 (${maxAttempts})，停止`);
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
          `连接断开: ${error}，${(actualDelay / 1000).toFixed(1)}秒后重连 ` +
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
      const ws = new WebSocket(this.config.serverUrl);
      this.ws = ws;

      ws.on('open', () => {
        const useForce = this.config.force || (this.wasConnectedBefore && this.consecutiveRejectCount > 0);
        const authMessage = createAuthMessage(this.config.token, useForce);
        ws.send(JSON.stringify(authMessage));
      });

      ws.on('message', async (data: Buffer) => {
        try {
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
              ws.send(JSON.stringify(createPongMessage()));
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
        // WebSocket 断开后服务端会重新分配 conn_id，旧本地连接全部丢弃，防止泄漏
        this.cleanupTcpConnections();
        const wasConnected = this.connected;
        this.connected = false;
        this.domain = null;
        // 无论是正常关闭还是异常关闭，只要之前是已连接状态，都需要触发 onDisconnect
        // 否则调用方（如 tunnelService）的状态会停留在 connected=true，导致 UI 显示误连接
        if (wasConnected) {
          this.events.onDisconnect?.();
        }
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
    this.connected = true;
    this.wasConnectedBefore = true;
    this.reconnectCount = 0;
    this.consecutiveRejectCount = 0;
    console.log(`已连接: domain=${this.domain}`);
    this.events.onConnect?.(this.domain);
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

  private async handleRequest(
    request: TunnelRequest,
    ws: WebSocket
  ): Promise<void> {
    this.events.onRequest?.(request);

    const startTime = Date.now();

    try {
      // 构建完整 URL
      const url = `${this.config.targetUrl.replace(/\/$/, '')}${request.path}`;

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

      // 自定义 undici Agent，确保 headersTimeout/bodyTimeout 与请求超时一致
      // 不设置的话 undici 默认 headersTimeout=300s，Agent 处理慢时会先于 AbortController 触发
      const dispatcher = new Agent({
        headersTimeout: timeoutMs,
        bodyTimeout: timeoutMs,
        connectTimeout: 30_000,
      });

      const fetchOptions: RequestInit = {
        method: request.method,
        headers: cleanHeaders,
        body: body,
      };

      try {
        const fetchResponse = await fetch(url, {
          ...fetchOptions,
          signal: controller.signal,
          // @ts-expect-error Node.js fetch supports undici dispatcher option
          dispatcher,
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

        // 普通响应：读取完整内容
        const responseBody = await fetchResponse.text();
        const durationMs = Date.now() - startTime;

        const response = createResponse(
          request.id,
          fetchResponse.status,
          responseBody,
          responseHeaders,
          undefined,
          durationMs
        );
        ws.send(JSON.stringify(response));
      } finally {
        clearTimeout(timeoutId);
        await dispatcher.close();
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

      ws.send(JSON.stringify(response));
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
    ws.send(JSON.stringify(startMsg));

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
          ws.send(JSON.stringify(chunkMsg));
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
    ws.send(JSON.stringify(endMsg));
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
    };
    this.tcpConnections.set(connId, state);

    state.socket.on('data', (data: Buffer) => {
      const msg: TcpDataMessage = {
        type: MessageType.TCP_DATA,
        conn_id: connId,
        data: data.toString('base64'),
        sequence: state.sequence++,
        timestamp: new Date().toISOString(),
      };
      this.sendToWs(ws, JSON.stringify(msg));
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
   * 处理来自服务端的 TCP 数据：解码后写入本地连接
   */
  private handleTcpData(message: TcpDataMessage): void {
    const state = this.tcpConnections.get(message.conn_id);
    if (!state) {
      console.warn(`收到未知 TCP 连接的数据: ${message.conn_id}`);
      return;
    }

    const data = Buffer.from(message.data, 'base64');
    state.socket.write(data);
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
    this.sendToWs(ws, JSON.stringify(msg));
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

  private sendToWs(ws: WebSocket, data: string): void {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(data);
    }
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
