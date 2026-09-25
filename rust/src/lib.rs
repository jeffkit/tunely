//! tunely — WebSocket 隧道客户端（Rust 版）
//!
//! 与服务端（python/tunely）及 Node 客户端（typescript/）共享同一 wire 协议：
//! 认证 + 心跳 + HTTP 转发（含 SSE 流式）+ TCP 隧道模式（tcp_connect/tcp_data/tcp_close）。

pub mod client;
pub mod protocol;
