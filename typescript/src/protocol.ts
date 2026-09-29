/**
 * WS-Tunnel 协议定义
 *
 * 协议版本: 2.0（能力协商 + binary_frames + chunked_http）
 */

import { CLIENT_VERSION } from './version';

export enum MessageType {
  // 认证
  AUTH = 'auth',
  AUTH_OK = 'auth_ok',
  AUTH_ERROR = 'auth_error',

  // 请求-响应
  REQUEST = 'request',
  RESPONSE = 'response',

  // 流式响应（SSE 支持）
  STREAM_START = 'stream_start',
  STREAM_CHUNK = 'stream_chunk',
  STREAM_END = 'stream_end',

  // TCP 模式
  TCP_CONNECT = 'tcp_connect',
  TCP_DATA = 'tcp_data',
  TCP_CLOSE = 'tcp_close',

  // 心跳
  PING = 'ping',
  PONG = 'pong',
}

// ============== 认证消息 ==============

export interface AuthMessage {
  type: MessageType.AUTH;
  token: string;
  client_version?: string;
  force?: boolean;
  /**
   * 协议 v2 能力协商：客户端支持的能力。缺省/空数组 = 不声明任何能力。
   * 铁律：客户端只许声明自己已实现的能力（声明了没实现 = 事故）。
   * T2 起客户端声明 ['binary_frames']（见 CLIENT_CAPABILITIES）。
   */
  capabilities?: string[];
}

export interface AuthOkMessage {
  type: MessageType.AUTH_OK;
  domain: string;
  tunnel_id: string;
  server_version?: string;
  /** 协议 v2：协商启用的能力 = 服务端注册表 ∩ 客户端声明；缺字段按空集合理解 */
  capabilities?: string[];
}

export interface AuthErrorMessage {
  type: MessageType.AUTH_ERROR;
  error: string;
  code?: string;
}

// ============== 请求-响应消息 ==============

export interface TunnelRequest {
  type: MessageType.REQUEST;
  id: string;
  method: string;
  path: string;
  headers: Record<string, string>;
  body?: string | null;
  timeout?: number;
  /**
   * 协议 v2 chunked_http（T3）：服务端放行非 SSE 大响应流式回传。
   * 仅 forward_stream 发 true；/forward、/t/ 缓冲分支恒为 false——
   * 客户端只在 stream_ok 请求上允许切流式（否则对端缓冲 future 超时）。
   * 键名与 wire 一致（snake_case，同 tunnel_id / duration_ms 先例）。
   */
  stream_ok?: boolean;
  timestamp?: string;
}

export interface TunnelResponse {
  type: MessageType.RESPONSE;
  id: string;
  status: number;
  headers: Record<string, string>;
  body?: string | null;
  error?: string | null;
  duration_ms?: number;
  timestamp?: string;
}

// ============== 流式响应消息（SSE 支持） ==============

export interface StreamStartMessage {
  type: MessageType.STREAM_START;
  id: string;
  status: number;
  headers: Record<string, string>;
  timestamp?: string;
}

export interface StreamChunkMessage {
  type: MessageType.STREAM_CHUNK;
  id: string;
  data: string;
  sequence?: number;
  /**
   * 协议 v2 chunked_http（T3）：data 编码。plain = UTF-8 文本
   * （SSE / text/* / application/json）；base64 = 二进制内容字节
   * （+33% 开销，v2 不给 stream 走 binary 帧，见 PROTOCOL_V2 §4）。
   * 缺省按 plain 理解（旧客户端兼容）。
   */
  encoding?: 'plain' | 'base64';
  timestamp?: string;
}

export interface StreamEndMessage {
  type: MessageType.STREAM_END;
  id: string;
  error?: string | null;
  duration_ms?: number;
  total_chunks?: number;
  timestamp?: string;
}

// ============== TCP 模式消息 ==============

export interface TcpConnectMessage {
  type: MessageType.TCP_CONNECT;
  conn_id: string;
  timestamp?: string;
}

export interface TcpDataMessage {
  type: MessageType.TCP_DATA;
  conn_id: string;
  /** Base64 编码的二进制数据 */
  data: string;
  sequence?: number;
  timestamp?: string;
}

export interface TcpCloseMessage {
  type: MessageType.TCP_CLOSE;
  conn_id: string;
  error?: string | null;
  timestamp?: string;
}

// ============== 心跳消息 ==============

export interface PingMessage {
  type: MessageType.PING;
  timestamp?: string;
}

export interface PongMessage {
  type: MessageType.PONG;
  timestamp?: string;
}

// ============== 消息联合类型 ==============

export type Message =
  | AuthMessage
  | AuthOkMessage
  | AuthErrorMessage
  | TunnelRequest
  | TunnelResponse
  | StreamStartMessage
  | StreamChunkMessage
  | StreamEndMessage
  | TcpConnectMessage
  | TcpDataMessage
  | TcpCloseMessage
  | PingMessage
  | PongMessage;

// ============== 辅助函数 ==============

/**
 * 本客户端已实现并声明的能力（协议 v2 能力协商）。
 * 铁律：只许声明已实现的能力（声明了没实现 = 服务端会用而客户端解析不了 = 事故）。
 * T2 起实现 binary_frames；T3 起实现 chunked_http（非 SSE 大响应流式回传）。
 * 新能力实现后在此追加。
 */
export const CLIENT_CAPABILITIES: string[] = ['binary_frames', 'chunked_http'];

export function createAuthMessage(
  token: string,
  force: boolean = false,
  capabilities: string[] = CLIENT_CAPABILITIES
): AuthMessage {
  return {
    type: MessageType.AUTH,
    token,
    // 真实版本上报（服务端 /api/tunnels 用于升级核对）；历史版本硬编码 '0.1.0' 是假值
    client_version: CLIENT_VERSION,
    force,
    capabilities,
  };
}

export function createPongMessage(): PongMessage {
  return {
    type: MessageType.PONG,
    timestamp: new Date().toISOString(),
  };
}

export function createResponse(
  requestId: string,
  status: number,
  body?: string | null,
  headers?: Record<string, string>,
  error?: string,
  durationMs?: number
): TunnelResponse {
  return {
    type: MessageType.RESPONSE,
    id: requestId,
    status,
    headers: headers || {},
    body,
    error,
    duration_ms: durationMs,
    timestamp: new Date().toISOString(),
  };
}

export function parseMessage(data: string): Message {
  const obj = JSON.parse(data);
  return obj as Message;
}
