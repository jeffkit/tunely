# WS-Tunnel 协议规范

**版本**: 2.0

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
| `udp_open` | Server → Client | 通知新 UDP 会话（UDP 模式，协议 v2） |
| `udp_close` | 双向 | UDP 会话关闭（UDP 模式，协议 v2） |
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

### 6. UDP 透传模式（协议 v2 udp 新增）

服务端经 `WS_TUNNEL_UDP_LISTEN` 监听 UDP 端口时，外部 UDP 数据报按
「监听端口 + 外部源地址」聚为会话，经 WebSocket 透传给隧道客户端转发到目标服务；
控制面用下列 JSON 消息，数据面走 `0x03` 二进制帧（见「UDP 会话语义」与「binary_frames 帧格式」）。

```
公网 UDP 客户端 ──► 服务端(监听端口) ──udp_open──► 隧道客户端 ──► 目标服务
                 ◄──0x03 udp_data 帧（双向）──►
                 ◄──udp_close (双向)──
```

#### udp_open（服务端 → 客户端）

```json
{
  "type": "udp_open",
  "session_id": "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b",
  "timestamp": "2026-09-29T02:00:06.000000+00:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `udp_open` |
| `session_id` | string | ✓ | 会话唯一 ID（36 字符 UUID），后续 `0x03` 帧 / `udp_close` 靠它关联 |
| `timestamp` | string | | 会话建立时间 |

#### udp_close（双向）

```json
{
  "type": "udp_close",
  "session_id": "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b",
  "reason": "idle timeout",
  "timestamp": "2026-09-29T02:00:36.000000+00:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✓ | 固定为 `udp_close` |
| `session_id` | string | ✓ | 要关闭的会话 ID |
| `reason` | string | | 关闭原因（如 `idle timeout` / `socket error`；可缺省） |
| `timestamp` | string | | 关闭时间 |

任一方收到 `udp_close` 后应停止该 `session_id` 上的收发并释放本地 socket；
服务端空闲超时回收与客户端 socket 错误收尾都会发送本消息。

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
| `udp` | UDP 会话透传（`udp_open`/`udp_close` 控制面 + `0x03` 帧数据面） | 已注册（v2 T4：服务端 + py/rust 客户端；**TS 未实现也不声明**，见「UDP 会话语义」） |

> 实现落地后才由服务端注册进 `SERVER_CAPABILITIES` 并同步本表；声明了未注册的能力不会被协商启用。
> 当前注册表：`["binary_frames", "chunked_http", "udp"]`。

### binary_frames 帧格式（已落地）

协商启用的连接上，`tcp_data` 双向改走 WS **binary** 帧（`tcp_close` 保持 JSON——低频且携带 error）：

```
[0x02]     1B  协议版本标记（v2）
[0x01]     1B  帧类型：0x01 = tcp_data
[16B]      conn_id，UUID v4 原始字节（JSON 控制面仍是 36 字符串形式）
[payload]  原始字节（无 base64、无 JSON）
```

- `sequence` 不进二进制帧：WS 消息本身有序，接收侧本就不依赖；
- 双向都用：服务端 TCP 读循环（外部→客户端）与客户端 TCP 读循环（目标→服务端）在能力启用时发 binary 帧；
- **未协商连接收到 binary 帧 → 丢弃 + warning**（F10 语义，不断连）；畸形帧（版本/类型/长度不对）同样丢弃；
- 未协商路径的 wire 行为与 1.x 完全一致（JSON + base64）。

协议 v2 T4 起新增帧类型 **`0x03 = udp_data`**（同一帧头布局，16B 为 `session_id`，
见「UDP 会话语义」）；接收侧按 `[1]` 帧类型分派，两种帧类型互斥不可互换解码。

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

### UDP 会话语义（已落地，协议 v2 T4）

服务端经 `WS_TUNNEL_UDP_LISTEN="port:domain[,port:domain...]"`（与 TCP 监听同格式、
独立 env）监听 UDP 端口，外部数据报按连接协商门控透传给隧道客户端：

- **会话键 = `(监听端口, 外部 addr)`**：同一外部地址在同一监听端口上的所有数据报
  归属同一会话（UDP 无连接，服务端按源地址区分「连接」）。首包建会话（uuid4）→
  先发 `udp_open` 再发 `0x03` 帧（WS 有序，客户端先建 socket 再收数据）；
  同源后续包直接 `0x03` 帧，不重复 `udp_open`。
- **数据面帧**：`0x03 = udp_data`，`[0x02][0x03][16B session_id][payload]`，
  **双向**，payload 为原始数据报字节（一帧一报文，不拼接）。回程：客户端 `0x03` 帧 →
  服务端查会话 → `sendto(外部 addr)`；session 不存在 → debug 丢弃。
  **不做 base64 JSON 数据变体**——协商了 udp 就必然有 binary frame 通道。
- **门控**：隧道未连接、或该连接未协商 `udp` 时收到外部 UDP 包 → 丢弃 + warning
  （旧客户端 × 新服务端 = 无此流量；服务端 UDP 监听默认不开）。
- **超时**：`WS_TUNNEL_UDP_SESSION_TIMEOUT`（默认 60s，0 = 不限）内无包的会话由
  周期 sweeper 回收：发 `udp_close(reason="idle timeout")` + 删映射；
  客户端先发 `udp_close` → 服务端删映射；隧道 WS 断连 → 该隧道全部会话直接清表
  （udp_close 发不到已断的客户端，客户端靠自身断连路径回收 socket）。
- **上限**：`WS_TUNNEL_UDP_MAX_SESSIONS`（默认 256/隧道，0 = 不限），超限丢包 + warning。
- **反射放大风险**：UDP 无连接，会话只把回程数据报发还给「已建立会话的外部 addr」，
  不响应未建会话的源；但公网开 UDP 监听仍需自行评估暴露面（扫描可触发
  udp_open + 上行流量，务必配合防火墙/端口映射白名单）。
- **UDP/TCP 同端口并存**：UDP 与 TCP 监听可用同一端口号（协议栈不同），配置互不干扰。
- **客户端实现**：py + rust 声明并实现；**TS 客户端本期不实现、不声明**——
  服务端对未协商连接本来就不投递 UDP 包，TS 客户端无感知。客户端语义：
  收 `udp_open` → 建到 target(host:port) 的 UDP socket（每会话一个）+ 起收包任务；
  收 `0x03` 帧 → `session.sendto(payload)`；socket 收到回包 → `0x03` 帧回服务端；
  socket 错误或收 `udp_close` / WS 断连 → 清理会话（socket 错误时主动回执 `udp_close`）。

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
  仅发生在服务端标记 `stream_ok` 的请求上；`stream_chunk` 增加可选 `encoding`）；
  第三个能力 `udp` 落地（UDP 会话透传：`udp_open`/`udp_close` 控制面 + `0x03` 帧数据面，
  会话键 = 监听端口 + 外部 addr，空闲超时回收与每隧道会话上限，TS 客户端不实现不声明）。
- **1.1**：新增 SSE 流式响应消息（`stream_start` / `stream_chunk` / `stream_end`）与 TCP 透传消息（`tcp_connect` / `tcp_data` / `tcp_close`）；`auth` 增加 `force` 抢占字段。
- **1.0**：认证、HTTP 请求-响应、心跳。
