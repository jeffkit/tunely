# tunely（Rust 客户端）

tunely 的 Rust 版**客户端**：单个静态二进制、零运行时依赖，适合在内网机器上分发部署。
与服务端（`python/`）和 Node 客户端（`typescript/`）共享同一 wire 协议。

## 功能

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
  [--reconnect 5] [--max-reconnect 0] [--request-timeout 300] [--force]
```

Ctrl-C 优雅停止。

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

## 测试

```bash
cargo test          # 6 单测（协议 roundtrip/退避/UTF-8 流解码）+ 8 集成测试
```

集成测试用真实 WebSocket（`accept_async` 起假服务端）+ 真实本地 TCP/HTTP 目标服务，
覆盖认证、双向转发、幂等关闭、目标拒绝、断线清理重连、force 抢占、HTTP/SSE 转发。

## 与其他客户端的差异

- 协议消息里的 `timestamp` 等纯元数据字段不发送（协议可选，服务端不强依赖）
- 其余 wire 行为与 `typescript/` 客户端逐语义对齐（含连续被拒累积退避、force 抢占）
