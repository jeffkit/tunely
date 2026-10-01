# tunely（Rust 客户端）

tunely 的 Rust 版**客户端**。同一份代码有三种用法：**单个静态二进制**（零运行时依赖，
适合内网机器分发部署）、**Rust 库**（`cargo add tunely` 进程内嵌入）、
**C ABI**（`cdylib`/`staticlib`，Go/C/C#/Java 也能进程内嵌入）。
与服务端（`python/`）和 Node 客户端（`typescript/`）共享同一 wire 协议。

## 功能

- **可嵌入**：`TunnelClient` + 进程内 `handler`（宿主自己应答请求，可不起本地端口）；
  另提供 C ABI（`include/tunely.h`）供非 Rust 宿主嵌入——见
  [`docs/EMBEDDING.md`](../docs/EMBEDDING.md)
- **HTTP 隧道模式**：转发服务端下发的 HTTP 请求到本地目标服务（hop-by-hop 头剥离、
  按请求 timeout 超时、503/504/500 错误映射，与 TS 客户端一致）
- **SSE 流式**：`Content-Type: text/event-stream` 自动走 `stream_start/stream_chunk/stream_end`，
  增量 UTF-8 解码（跨 chunk 多字节序列安全）
- **TCP 隧道模式**：`tcp_connect/tcp_data/tcp_close` 全量支持（并发连接、base64 双向转发、
  sequence 递增、幂等 tcp_close、服务端关闭不回执、WS 断开时丢弃全部本地连接）
- **重连**：干净断开固定间隔；错误指数退避（factor 封顶 8、延迟封顶 5 分钟、±20% 抖动）；
  认证被拒（already connected）后自动带 `force` 抢占（与 TS 客户端一致的
  wasConnectedBefore 语义）

## CLI

```bash
cargo build --release
# 二进制：target/release/tunely（约 5MB，strip + lto）

tunely connect \
  --server wss://your-server/ws/tunnel \
  --token tun_xxxxx \
  --target http://127.0.0.1:3080 \
  [--reconnect 5] [--max-reconnect 0] [--request-timeout 300] [--force] [--config path/to/client.toml]

tunely status   # 查看运行中的客户端状态
```

Ctrl-C 优雅停止。

## 配置与凭据

`connect` 的 `--token/--server/--target` 均可省略，取值优先级：
**CLI 参数 > 环境变量 > 配置文件 > 内置默认**（server=`ws://localhost:8000/ws/tunnel`，
target=`http://localhost:8080`）。三处都拿不到 token 时报错退出（退出码 1）。

### 环境变量

```bash
export TUNELY_TOKEN=tun_xxxxx
export TUNELY_SERVER=wss://your-server/ws/tunnel
export TUNELY_TARGET=http://127.0.0.1:3080
tunely connect
```

### 配置文件（TOML）

```bash
# 显式指定（文件必须存在）
tunely connect --config /etc/tunely/client.toml
# 省略时依次尝试 ./tunely-client.toml 与 ~/.config/tunely/client.toml（存在才读取，全缺省静默跳过）
```

```toml
# tunely-client.toml —— 所有字段均可选
server = "wss://your-server/ws/tunnel"
token = "tun_xxxxx"
target = "http://127.0.0.1:3080"
proxy = "http://127.0.0.1:7890"   # 可选：出站 HTTP CONNECT 代理（见下「代理出站」）
reconnect_secs = 5
max_reconnect = 0        # 0 = 无限
request_timeout_secs = 300
force = false
```

### 代理出站（HTTP CONNECT，0.6.0+）

跨境直连受限时，客户端 → server 的 WS 可经 HTTP CONNECT 代理转发：

```toml
proxy = "http://proxy.lan:7890"
```

```bash
# 环境变量回退（HTTPS_PROXY > https_proxy > ALL_PROXY > all_proxy）
export HTTPS_PROXY=http://proxy.lan:7890
tunely connect
```

- 优先级：**配置文件 > 代理 env > 无**（注意与 TUNELY_* 的 CLI > env > file
  顺序不同：站点级配置覆盖部署环境注入的通用代理变量）。
- **作用域**：只作用于「客户端 → server」的 WS 出站；转发目标（target）的
  流量语义不变，不经代理。wss 场景 TLS 在 CONNECT 隧道内部完成，端到端加密不变。
- **v1 仅支持 HTTP CONNECT 代理，SOCKS 明确不支持**（配置了 socks 启动即报错）；
  对代理本身走 TLS（`https://` 代理）与代理认证（userinfo）暂不支持。
- 代理失败（不可达/拒绝 CONNECT）走既有重连退避语义；启动横幅会打印生效的代理。

### status 子命令与状态文件

`connect` 运行期间在状态变化点（连接成功、断开进入重连、出错、停止）把状态写入状态文件，
路径取 `TUNELY_STATE_FILE` 环境变量，缺省为 `~/.local/state/tunely/status.json`（目录自动创建）：

```json
{"pid": 4242, "state": "connected", "domain": "dsh.example.com",
 "reconnect_count": 0, "last_error": null, "updated_at": "2026-09-25T03:00:00.123456Z"}
```

```bash
$ tunely status
tunely 状态
  pid:             4242
  state:           connected
  domain:          dsh.example.com
  reconnect_count: 0
  last_error:      -
  updated_at:      2026-09-25T03:00:00.123456Z

$ tunely status   # 状态文件不存在时
tunely: not running   （退出码 1）
```

## SDK 用法

```rust
use std::sync::{Arc,atomic::{AtomicBool, Ordering}};
use std::time::Duration;
use tunely::client::{TunnelClient, TunnelClientConfig};

#[tokio::main]
async fn main() {
    let client = TunnelClient::new(TunnelClientConfig {
        server_url: "wss://your-server/ws/tunnel".into(),
        token: "tun_xxxxx".into(),
        target_url: "http://127.0.0.1:3080".into(),
        reconnect_interval: Duration::from_secs(5),
        ..Default::default()
    });

    client.on_connect(|domain| println!("已连接: {domain}"));
    client.on_disconnect(|| println!("断开"));

    let c = Arc::new(client);
    let c2 = c.clone();
    tokio::spawn(async move {
        let _ = tokio::signal::ctrl_c().await;
        c2.stop();
    });
    c.run().await;
}
```

## 进程内 handler：宿主自己应答，不必监听本地端口

装上 handler 后，服务端下发的每条 HTTP 请求先交给它；返回 `Forward` 才回落到
`target_url`（与不装 handler 时逐字节一致）：

```rust
use tunely::handler::{sync_handler, HandlerOutcome, HandlerResponse};

let client = TunnelClient::new(TunnelClientConfig {
    server_url: "wss://your-server/ws/tunnel".into(),
    token: "tun_xxxxx".into(),
    target_url: "http://127.0.0.1:9".into(), // 仅作回落兜底
    ..Default::default()
})
.with_request_handler(sync_handler(|req| match req.path.as_str() {
    "/health" => HandlerResponse::json(200, r#"{"ok":true}"#).into(),
    _ => HandlerOutcome::Forward,
}));

client.run().await;
```

流式响应（SSE / LLM token 逐块回传）用 `HandlerOutcome::Stream(HandlerStream::new(..))`，
按 `stream_start` / `stream_chunk*` / `stream_end` 发送；chunk 流里的 `Err(msg)` 会写进
`stream_end.error` 并终止。异步 handler 自行 `impl RequestHandler` 或用 `ClosureHandler`。

- 只作用于 **HTTP 模式**；TCP/UDP 是裸字节流，不经 handler
- `handle` 在 tokio 工作线程上被 await，阻塞操作请自己 `spawn_blocking`
- handler 在连接建立时快照；`run()` 之后设置需重连才生效
- 嵌入式客户端不写状态文件（`tunely status` 看不到它）

## C ABI：让 Go / C / C# / Java 也能进程内嵌入

`crate-type` 含 `cdylib` + `staticlib`，`cargo build --release` 产出
`libtunely.dylib` / `libtunely.so` / `libtunely.a`，接口见 `include/tunely.h`，
可运行示例见 `c/embed_demo.c`：

```bash
cargo build --release
cc c/embed_demo.c -I include -L target/release -ltunely -o /tmp/embed_demo
python3 c/fake_server.py 8795 &   # 单连接假服务端，校验 C 宿主回包
DYLD_LIBRARY_PATH=target/release /tmp/embed_demo ws://127.0.0.1:8795/ws/tunnel tun_demo
```

内存归属是单向的：库不返回需要宿主 `free` 的指针，宿主写回的 body 在回调返回前被立即
拷贝（宿主可用任意分配器）。完整契约见 `include/tunely.h` 顶部注释与
[`docs/EMBEDDING.md`](../docs/EMBEDDING.md)。

## 测试

```bash
cargo test     # 61 单测（协议 roundtrip/退避/UTF-8 流解码、配置解析、状态文件、UDP 会话）
               # + 24 集成测试：integration(10) / wire_conformance(8) / embed_handler(6)
cargo clippy --all-targets -- -D warnings
```

集成测试用真实 WebSocket（`accept_async` 起假服务端）+ 真实本地 TCP/HTTP 目标服务，
覆盖认证、双向转发、幂等关闭、目标拒绝、断线清理重连、force 抢占、HTTP/SSE 转发；
`embed_handler` 另外覆盖进程内 handler 的缓冲/流式/回落三条路径与 C ABI 端到端
（`target_url` 故意指向不可达端口，证明 handler 应答不依赖本地目标服务）。

## 与其他客户端的差异

- 协议消息里的 `timestamp` 等纯元数据字段不发送（协议可选，服务端不强依赖）
- **无 WS 压缩**：底层的 tokio-tungstenite 0.24 不支持 permessage-deflate 扩展，
  Rust 客户端不启用 WebSocket 压缩
- 其余 wire 行为与 `typescript/` 客户端逐语义对齐（含连续被拒累积退避、force 抢占）
