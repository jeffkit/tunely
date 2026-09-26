# WS-Tunnel 协议规范

**版本**: 1.1

## 概述

WS-Tunnel 协议定义了服务端和客户端之间的通信格式，基于 WebSocket 传输 JSON 消息。

## 消息类型

| 类型 | 方向 | 说明 |
|------|------|------|
| `auth` | Client → Server | 认证请求 |
| `auth_ok` | Server → Client | 认证成功 |
| `auth_error` | Server → Client | 认证失败 |
| `request` | Server → Client | HTTP 请求 |
| `response` | Client → Server | HTTP 响应（完整响应） |
| `stream_start` | Client → Server | 流式响应开始（SSE） |
| `stream_chunk` | Client → Server | 流式响应数据块（SSE） |
| `stream_end` | Client → Server | 流式响应结束（SSE） |
| `tcp_connect` | Server → Client | 通知新 TCP 连接（TCP 模式） |
| `tcp_data` | 双向 | TCP 数据传输（TCP 模式） |
| `tcp_close` | 双向 | TCP 连接关闭（TCP 模式） |
| `ping` | Server → Client | 心跳请求 |
| `pong` | Client → Server | 心跳响应 |

> 跨实现一致性用例见 [`spec/conformance/wire.json`](../spec/conformance/wire.json)（py / rust / ts 三端共用）。

## 消息格式

### 1. 认证阶段

#### auth（客户端 → 服务端）

```json
{
  "type": "auth",
  "token": "tun_xxxxxxxxxxxxx",
  "client_version": "0.1.0",
  "force": false
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `auth` |
| `token` | string | ✓ | 隧道令牌 |
| `client_version` | string | | 客户端版本 |
| `force` | boolean | | 是否强制抢占已有连接（默认 `false`；`true` 时服务端会踢掉当前连接） |

#### auth_ok（服务端 → 客户端）

```json
{
  "type": "auth_ok",
  "domain": "my-agent",
  "tunnel_id": "123",
  "server_version": "0.1.0"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `auth_ok` |
| `domain` | string | ✓ | 分配的域名 |
| `tunnel_id` | string | ✓ | 隧道 ID |
| `server_version` | string | | 服务端版本 |

#### auth_error（服务端 → 客户端）

```json
{
  "type": "auth_error",
  "error": "Invalid token",
  "code": "auth_failed"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `auth_error` |
| `error` | string | ✓ | 错误信息 |
| `code` | string | | 错误代码 |

### 2. 请求-响应阶段

#### request（服务端 → 客户端）

```json
{
  "type": "request",
  "id": "req-001",
  "method": "POST",
  "path": "/api/chat",
  "headers": {
    "Content-Type": "application/json",
    "Authorization": "Bearer xxx"
  },
  "body": "{\"message\": \"hello\"}",
  "timeout": 300,
  "timestamp": "2024-01-17T12:00:00.000Z"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `request` |
| `id` | string | ✓ | 请求唯一 ID |
| `method` | string | ✓ | HTTP 方法 |
| `path` | string | ✓ | 请求路径 |
| `headers` | object | | HTTP 请求头 |
| `body` | string | | 请求体（JSON 字符串） |
| `timeout` | number | | 超时时间（秒） |
| `timestamp` | string | | 请求时间（ISO 8601） |

#### response（客户端 → 服务端）

```json
{
  "type": "response",
  "id": "req-001",
  "status": 200,
  "headers": {
    "Content-Type": "application/json"
  },
  "body": "{\"response\": \"hi\"}",
  "duration_ms": 150,
  "timestamp": "2024-01-17T12:00:00.150Z"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `response` |
| `id` | string | ✓ | 对应的请求 ID |
| `status` | number | ✓ | HTTP 状态码 |
| `headers` | object | | HTTP 响应头 |
| `body` | string | | 响应体 |
| `error` | string | | 错误信息（如果请求失败） |
| `duration_ms` | number | | 请求耗时（毫秒） |
| `timestamp` | string | | 响应时间 |

### 3. 流式响应阶段（SSE，v1.1 新增）

当客户端检测到上游响应为 SSE（`Content-Type: text/event-stream`）时，不再回单个 `response`，
改按 `stream_start` → `stream_chunk`* → `stream_end` 三段推送；三者 `id` 均对应原 `request.id`。

#### stream_start（客户端 → 服务端）

```json
{
  "type": "stream_start",
  "id": "req-sse",
  "status": 200,
  "headers": {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache"
  },
  "timestamp": "2026-09-25T02:00:01.000000+00:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `stream_start` |
| `id` | string | ✓ | 对应的请求 ID |
| `status` | number | ✓ | HTTP 状态码 |
| `headers` | object | | HTTP 响应头 |
| `timestamp` | string | | 开始时间 |

#### stream_chunk（客户端 → 服务端）

```json
{
  "type": "stream_chunk",
  "id": "req-sse",
  "data": "data: hello\n\n",
  "sequence": 2,
  "timestamp": "2026-09-25T02:00:01.100000+00:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `stream_chunk` |
| `id` | string | ✓ | 对应的请求 ID |
| `data` | string | ✓ | 数据块内容（一个 SSE 事件帧） |
| `sequence` | number | | 数据块序号，从 0 递增 |
| `timestamp` | string | | 发送时间 |

#### stream_end（客户端 → 服务端）

```json
{
  "type": "stream_end",
  "id": "req-sse",
  "error": null,
  "duration_ms": 1500,
  "total_chunks": 3,
  "timestamp": "2026-09-25T02:00:02.000000+00:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `stream_end` |
| `id` | string | ✓ | 对应的请求 ID |
| `error` | string \| null | | 异常结束时的错误信息；正常结束为 `null` |
| `duration_ms` | number | | 流总耗时（毫秒） |
| `total_chunks` | number | | 数据块总数 |
| `timestamp` | string | | 结束时间 |

### 4. TCP 透传模式（v1.1 新增）

隧道模式为 `tcp` 时（`tunely tunnel create <domain> --mode tcp`，服务端经 `WS_TUNNEL_TCP_LISTEN`
监听端口），原始 TCP 字节流按连接封装为下列消息经 WebSocket 透传；`data` 为 Base64 编码。

```
公网 TCP 客户端 ──► 服务端(监听端口) ──tcp_connect──► 隧道客户端
                 ◄──tcp_data (双向)──►
                 ◄──tcp_close (双向)──
```

#### tcp_connect（服务端 → 客户端）

```json
{
  "type": "tcp_connect",
  "conn_id": "conn-7f3a",
  "timestamp": "2026-09-25T02:00:03.000000+00:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `tcp_connect` |
| `conn_id` | string | ✓ | 连接唯一 ID，后续 `tcp_data` / `tcp_close` 靠它关联 |
| `timestamp` | string | | 连接建立时间 |

#### tcp_data（双向）

```json
{
  "type": "tcp_data",
  "conn_id": "conn-7f3a",
  "data": "aGVsbG8gdGNw",
  "sequence": 5,
  "timestamp": "2026-09-25T02:00:03.200000+00:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `tcp_data` |
| `conn_id` | string | ✓ | 连接 ID |
| `data` | string | ✓ | Base64 编码的二进制数据 |
| `sequence` | number | | 数据包序号（每连接从 0 递增） |
| `timestamp` | string | | 发送时间 |

#### tcp_close（双向）

```json
{
  "type": "tcp_close",
  "conn_id": "conn-7f3a",
  "error": null,
  "timestamp": "2026-09-25T02:00:04.000000+00:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `tcp_close` |
| `conn_id` | string | ✓ | 要关闭的连接 ID |
| `error` | string \| null | | 异常关闭时的错误信息；正常关闭为 `null` |
| `timestamp` | string | | 关闭时间 |

任一方收到 `tcp_close` 后应停止该 `conn_id` 上的收发并释放本地连接；`error` 非空表示对端/上游异常断开。

### 5. 心跳阶段

#### ping（服务端 → 客户端）

```json
{
  "type": "ping",
  "timestamp": "2024-01-17T12:00:00.000Z"
}
```

#### pong（客户端 → 服务端）

```json
{
  "type": "pong",
  "timestamp": "2024-01-17T12:00:00.000Z"
}
```

## 连接流程（HTTP 模式）

```
Client                                  Server
   |                                       |
   |-------- WebSocket Connect ----------->|
   |                                       |
   |-------- auth {token} ---------------->|
   |                                       |
   |<------- auth_ok {domain} -------------|
   |         或 auth_error                 |
   |                                       |
   |========= 已认证，等待请求 =============|
   |                                       |
   |<------- request {id, method, ...} ----|
   |                                       |
   |        (执行本地 HTTP 请求)            |
   |                                       |
   |-------- response {id, status, ...} -->|
   |                                       |
   |<------- ping --------------------------|
   |-------- pong ------------------------->|
   |                                       |
   |         (保持连接)                     |
   |                                       |
```

## 错误处理

### 认证错误

| 错误代码 | 说明 |
|----------|------|
| `auth_failed` | 令牌无效 |
| `tunnel_disabled` | 隧道已禁用 |
| `auth_timeout` | 认证超时 |

### 请求错误

客户端应在 `response.error` 中返回错误信息：

```json
{
  "type": "response",
  "id": "req-001",
  "status": 504,
  "error": "Target service timeout"
}
```

常见状态码：
- `503`: 目标服务不可用
- `504`: 请求超时
- `500`: 内部错误

## 版本兼容

- 客户端和服务端通过 `client_version` / `server_version` 字段交换版本信息
- 服务端应向后兼容旧版本客户端
- 客户端应忽略未知的消息字段

## 版本历史

- **1.1**：新增 SSE 流式响应消息（`stream_start` / `stream_chunk` / `stream_end`）与 TCP 透传消息（`tcp_connect` / `tcp_data` / `tcp_close`）；`auth` 增加 `force` 抢占字段。
- **1.0**：认证、HTTP 请求-响应、心跳。
