/**
 * WS-Tunnel 协议定义
 *
 * 协议版本: 1.0
 */

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
}

export interface AuthOkMessage {
  type: MessageType.AUTH_OK;
  domain: string;
  tunnel_id: string;
  server_version?: string;
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
  | PingMessage
  | PongMessage;

// ============== 辅助函数 ==============

export function createAuthMessage(token: string, force: boolean = false): AuthMessage {
  return {
    type: MessageType.AUTH,
    token,
    client_version: '0.1.0',
    force,
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
