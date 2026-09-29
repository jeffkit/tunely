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
| `capabilities` | string[] | | 客户端支持的能力（协议 v2 协商，见「能力协商」；缺省 = 空集合） |

#### auth_ok（服务端 → 客户端）

```json
{
  "type": "auth_ok",
  "domain": "my-agent",
  "tunnel_id": "123",
  "server_version": "0.1.0",
  "capabilities": ["binary_frames"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `auth_ok` |
| `domain` | string | ✓ | 分配的域名 |
| `tunnel_id` | string | ✓ | 隧道 ID |
| `server_version` | string | | 服务端版本 |
| `capabilities` | string[] | | 协商启用的能力（协议 v2，见「能力协商」；缺省 = 空集合） |

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
| `stream_ok` | boolean | | 服务端放行非 SSE 大响应流式回传（协议 v2 chunked_http，见「chunked_http 语义」）；缺省 = `false` |
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
| `encoding` | string | | `data` 的编码：`plain`（UTF-8 文本，缺省）或 `base64`（二进制内容字节，协议 v2 chunked_http，见「chunked_http 语义」） |
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

## 能力协商（协议 v2）

`auth` 与 `auth_ok` 携带可选的 `capabilities: string[]`，用于在认证时协商本连接启用哪些新行为。
机制先于内容：协商通道自 2.0 起生效，具体能力随实现逐步注册。

**规则（铁律）：**

1. **字段缺失 = 空集合**。任何一端不带 `capabilities` 都按「未声明任何能力」理解，行为与旧版本完全一致。
2. **只回交集**。`auth_ok.capabilities` = 服务端能力注册表 ∩ 客户端声明 − 服务端 kill 开关禁用集
   （`WS_TUNNEL_DISABLE_CAPABILITIES`，逗号分隔）。客户端声明了但服务端未注册（未实现）的能力不会出现在结果里。
3. **客户端只许声明自己已实现的能力**。声明了没实现 = 服务端会用而客户端解析不了 = 事故。
   新客户端版本把已实现的能力名加入声明；未实现前保持空集合/缺省。
4. **新行为必须门控**。只有 `auth_ok.capabilities` 里出现的能力，双方才可启用对应新行为；
   未协商路径保持旧版本行为不变。
5. **命名规则**：能力名为小写下划线（snake_case），如 `binary_frames`；新增能力必须先在下表登记。

**能力登记表：**

| 能力名 | 说明 | 状态 |
|--------|------|------|
| `binary_frames` | 数据面（`tcp_data`）改走 WS binary 帧，去掉 base64+JSON 开销 | 已注册（v2 T2，见下节） |
| `chunked_http` | 非 SSE 大响应按 `stream_start/chunk/end` 分块流式 | 已注册（v2 T3：服务端 + py/ts 客户端；rust 客户端不实现也不声明，见「chunked_http 语义」） |

> 实现落地后才由服务端注册进 `SERVER_CAPABILITIES` 并同步本表；声明了未注册的能力不会被协商启用。
> 当前注册表：`["binary_frames", "chunked_http"]`。

### binary_frames 帧格式（已落地）

协商启用的连接上，`tcp_data` 双向改走 WS **binary** 帧（`tcp_close` 保持 JSON——低频且携带 error）：

```
[0x02]     1B  协议版本标记（v2）
[0x01]     1B  帧类型：0x01 = tcp_data（当前唯一类型）
[16B]      conn_id，UUID v4 原始字节（JSON 控制面仍是 36 字符串形式）
[payload]  原始字节（无 base64、无 JSON）
```

- `sequence` 不进二进制帧：WS 消息本身有序，接收侧本就不依赖；
- 双向都用：服务端 TCP 读循环（外部→客户端）与客户端 TCP 读循环（目标→服务端）在能力启用时发 binary 帧；
- **未协商连接收到 binary 帧 → 丢弃 + warning**（F10 语义，不断连）；畸形帧（版本/类型/长度不对）同样丢弃；
- 未协商路径的 wire 行为与 1.x 完全一致（JSON + base64）。

### chunked_http 语义（已落地）

非 SSE 大响应（超过客户端流式阈值）改按 `stream_start` → `stream_chunk`* → `stream_end`
分块回传，避免两端全量缓冲。**非 SSE 流式仅发生在 `stream_ok` 请求上**：

- 服务端只有 `forward_stream`（流式 API）发出的 `request` 带 `stream_ok: true`；
  `/forward`、`/t/` 缓冲分支恒为 `false`——这些路径的缓冲 future 只认 `response`，
  客户端对未放行请求切流式 = 对端超时，因此客户端必须三条件齐备才切流式：
  **`request.stream_ok == true` ∧ 本连接协商了 `chunked_http` ∧ 响应体超过阈值**
  （阈值可配：py `WS_TUNNEL_CLIENT_STREAM_THRESHOLD_BYTES` / ts `TUNELY_STREAM_THRESHOLD_BYTES`，
  默认 1MB，`0` = 从不流式）；
- SSE 响应（`Content-Type: text/event-stream`）与现状一致，总是流式，不依赖 `stream_ok`；
- 块编码（`stream_chunk.encoding`）：`Content-Type` 为 `text/*` 或 `application/json`
  → UTF-8 文本 `plain`；其余（二进制）→ `base64`。**base64 +33% 开销是已知边界**
  （v2 不给 stream 走 binary 帧，见 PROTOCOL_V2 §4）；
- `Content-Length` 未知时读到一半越过阈值 → 已缓冲部分作为首批数据块就地切换；
- 客户端未协商该能力、或服务端是旧版本时，`stream_ok` 被忽略，行为与 1.x 完全一致
  （旧客户端照回 `response`，服务端会把它合成为单块流——非 SSE 目标经 `forward_stream`
  也能正常拿到全量响应，不再干等到超时）；
- **rust 客户端不实现本能力、也不声明**：即便收到 `stream_ok: true` 也照旧缓冲回
  `response`（字段按缺省 `false` 容忍；服务端补桥兜底）。

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

- **2.0**：能力协商机制（`auth`/`auth_ok` 增加可选 `capabilities`，缺字段 = 空集合，只回交集）；
  首个能力 `binary_frames` 落地（`tcp_data` 双向改走 WS binary 帧，`tcp_close` 保持 JSON，未协商行为不变）；
  第二个能力 `chunked_http` 落地（非 SSE 大响应经 `stream_start/chunk/end` 流式回传，
  仅发生在服务端标记 `stream_ok` 的请求上；`stream_chunk` 增加可选 `encoding`）。
- **1.1**：新增 SSE 流式响应消息（`stream_start` / `stream_chunk` / `stream_end`）与 TCP 透传消息（`tcp_connect` / `tcp_data` / `tcp_close`）；`auth` 增加 `force` 抢占字段。
- **1.0**：认证、HTTP 请求-响应、心跳。
