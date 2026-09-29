# Protocol v2 设计定稿（issue #3 的落地 spec，UDP=#4 不在本期）

> 状态：v1（2026-09-29，作为子任务实现的唯一权威依据）
> 原则：**控制面 JSON 不动；一切新行为必须协商门控；旧×新双向共存零破坏**
> 分期落地：T1 capabilities → T2 binary_frames → T3 chunked_http（顺序执行，每步全绿再下一步）

## 0. 能力协商（T1）

- `AuthMessage` 增加可选 `capabilities: list[str]`（客户端支持的能力）；
  `AuthOkMessage` 增加可选 `capabilities: list[str]`（**= 服务端注册表 ∩ 客户端声明**，只回交集）。
- 铁律（写入 PROTOCOL.md）：**字段缺失 = 空集合**；只有双方都声明的能力才启用；
  能力名小写下划线，新增须在 PROTOCOL.md 能力登记表注册；
  **客户端只许声明自己已实现的能力**（声明了没实现 = 服务端会用而客户端解析不了 = 事故）。
- 服务端注册表：`SERVER_CAPABILITIES` 常量，初始为 `[]`；T2 实现后加 `"binary_frames"`，T3 实现后加 `"chunked_http"`。
  kill 开关：`WS_TUNNEL_DISABLE_CAPABILITIES`（逗号分隔，从注册表剔除）。
- 三端客户端本期（T1）一律发空集合或不发；T2/T3 各自在实现完成后把自己的能力名加进客户端声明。
- 兼容性依据（已核实）：pydantic 默认 ignore extra；TS interface 可选字段；rust 加 `#[serde(default)]`（缺字段安全，照 server_version 先例）。

## 1. binary_frames（T2）——issue #3 草图的落地

采用 issue #3 的帧布局（jeffkit 提案），细节定稿：

- WS **binary** 帧（仅 `tcp_data` 一个类型）：
  ```
  [0x02]        1B  协议版本标记（v2）
  [0x01]        1B  帧类型：0x01 = tcp_data
  [16B]         conn_id，UUID v4 原始字节（JSON 控制面仍是 36 字符串形式）
  [payload]     原始字节（无 base64、无 JSON）
  ```
- `tcp_close` 保持 JSON（携带 error 字段，低频）；`sequence` 不进二进制帧（WS 有序，接收侧本就不依赖）。
- 双向都用：服务端 `_tcp_read_loop`（外部→客户端）与客户端 TCP 读循环（目标→服务端）在能力启用时发 binary 帧；
  服务端 WS 循环改 `receive()` 分派 text（JSON 控制面）/binary（数据面）。
- 门控：该连接 AuthOk 回了 `binary_frames` 才启用；未协商时收到 binary 帧 → 丢弃 + warning（F10 语义）。
- 兼容：未协商路径的代码保持 0.7.x 原样不动（老路径一行不改是验收项）。

## 2. chunked_http（T3）——非 SSE 大响应流式

背景（侦察结论，必须写进实现者的脑子）：
- `forward_stream` 今天对非 SSE 目标**本来就是坏的**：客户端回 `TunnelResponse` 只会完成缓冲型 future，流式消费侧干等超时（docstring 承诺的「完整响应 SingleChunk」从未实现）。T3 顺带修掉这个缺口。
- 客户端不能自作主张切流式：`/forward`、`/t/` 的缓冲分支在等 TunnelResponse future，客户端改发流 = 对端超时。**必须由服务端在请求上标记放行。**

设计：
- `TunnelRequest` 增加可选 `stream_ok: bool = false`（additive；由 `forward_stream` 发出，缓冲 API 永远不发）。
- `StreamChunkMessage` 增加可选 `encoding: "plain" | "base64"`，默认 `"plain"`（additive；二进制内容用 base64 块 + 标记，v2 不给 stream 走 binary 帧，+33% 局限记入 PROTOCOL.md 已知边界）。
- 客户端行为（py + TS；rust 无 HTTP 转发模式不涉及）：
  - SSE：与现状一致，总是流式（不需要 stream_ok）；
  - 非 SSE：仅当 **`request.stream_ok == true`** 且 **协商了 `chunked_http`** 且 **响应体 > 阈值**（config，py `WS_TUNNEL_CLIENT_STREAM_THRESHOLD_BYTES` / ts `TUNELY_STREAM_THRESHOLD_BYTES`，默认 1MB，0=从不流式）才切流式：
    发 `StreamStart(status, headers)` → 数据块（文本解码 plain / 二进制 base64+标记）→ `StreamEnd(total_chunks, duration_ms)`。
    Content-Length 未知的响应读到一半越过阈值 → 把已缓冲部分作为首批数据块就地切换。
  - 其余情况照旧回 `TunnelResponse`（0.7.3 的客户端 cap 语义保留）。
- 服务端：`forward_stream` 的请求带 `stream_ok=true`；**WS 循环补桥**：`TunnelResponse` 到达时若 id 在 `_pending_stream_requests` → 合成 `StreamStart + StreamChunk(全量 body, plain) + StreamEnd` 投入该流队列并清理（修复上述既有缺口）。
- `/forward`、`/t/` 缓冲分支零改动（stream_ok 门控保证它们永远不会收到流式回答）。
- app.py `/t/` 的大文件代理（对非 SSE 目标发 stream_ok）= 后续项，不在本期。

## 3. 版本与发布

- 实现期间不 bump 版本（避免三次 churn）；T3 验收后统一 py **0.8.0** / ts **0.3.0** / rust **0.2.0**，tag `python-v0.8.0`。
- 部署顺序（ROLLING_UPGRADE.md 纪律）：服务端先行（全兼容旧客户端）→ rust 客户端经 im-agentproc workflow 构建换二进制 → 逐隧道滚动；任何时刻新旧组合都通。

## 4. 已知边界（写进 PROTOCOL.md）

- stream 块走 base64（+33%）而非 binary 帧：SSE 是文本为主，大块二进制留给 v2.1 再评估 binary stream 帧。
- 小体积二进制 body 经 HTTP 模式仍有 UTF-8 replace 损坏（F4 未根治——只有超过流式阈值才走 base64 流）。彻底修 = 「协商了 chunked_http 就对非文本 content-type 一律流式」，留待后续（本期阈值规则简单优先）。
