//! tunely — WebSocket 隧道客户端（Rust 版）
//!
//! 与服务端（python/tunely）及 Node 客户端（typescript/）共享同一 wire 协议：
//! 认证 + 心跳 + HTTP 转发（含 SSE 流式）+ TCP 隧道模式（tcp_connect/tcp_data/tcp_close）。
//!
//! 出站连接支持经 HTTP CONNECT 代理（客户端 → server 的 WS；见 [`proxy`]，
//! 与 TS 客户端同规格：配置 > env 回退，v1 仅 http:// CONNECT、SOCKS 不支持）。
//!
//! 三种嵌入形态：
//! 1. 命令行（`[[bin]]`，见 `main.rs`）
//! 2. Rust 库：`TunnelClient` + [`handler`] 的进程内请求处理器——宿主可以不起
//!    本地端口，直接用自己的函数应答隧道请求
//! 3. C ABI（[`ffi`]，cdylib/staticlib）：Go / C / C# / Java 等宿主进程内嵌入

pub mod client;
pub mod config;
pub mod ffi;
pub mod handler;
pub mod proxy;
pub mod protocol;
pub mod status;

