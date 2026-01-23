# WS-Tunnel 协议规范

**版本**: 1.0

## 概述

WS-Tunnel 协议定义了服务端和客户端之间的通信格式，基于 WebSocket 传输 JSON 消息。

## 消息类型

| 类型 | 方向 | 说明 |
|------|------|------|
| `auth` | Client → Server | 认证请求 |
| `auth_ok` | Server → Client | 认证成功 |
| `auth_error` | Server → Client | 认证失败 |
| `request` | Server → Client | HTTP 请求 |
| `response` | Client → Server | HTTP 响应 |
| `ping` | Server → Client | 心跳请求 |
| `pong` | Client → Server | 心跳响应 |

## 消息格式

### 1. 认证阶段

#### auth（客户端 → 服务端）

```json
{
  "type": "auth",
  "token": "tun_xxxxxxxxxxxxx",
  "client_version": "0.1.0"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `auth` |
| `token` | string | ✓ | 隧道令牌 |
| `client_version` | string | | 客户端版本 |

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

### 3. 心跳阶段

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

## 连接流程

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
