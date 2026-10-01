# Changelog

本文件记录 tunely（rust/ 包，crates.io: tunely）的显著变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.6.0] - 2026-10-12

### Added

- **客户端出站支持 HTTP CONNECT 代理**（客户端 → server 的 WS 连接可经代理转发，
  跨境直连受限场景下可切更快的代理线路）：
  - 配置面（与 TS 客户端同规格）：配置文件 `proxy = "http://host:port"`
    （`client.toml` 顶层字段，多隧道形态下全局共享）；环境变量回退
    `HTTPS_PROXY` / `https_proxy` / `ALL_PROXY` / `all_proxy`
    （wss:// 语义按 https 处理）；**优先级：配置 > env > 无**
    （注意与 TUNELY_* 的 CLI > env > file 顺序不同：站点级配置覆盖部署环境
    通用变量）。v1 不设 CLI 旗标。
  - 实现路径：配置代理时 TCP 连代理 → 发 `CONNECT host:port HTTP/1.1`
    （authority 取自 server URL）→ 校验 2xx → 把已建立的隧道流交给
    `client_async_tls`；**wss 的 TLS 在 CONNECT 隧道内部完成，端到端加密不变**。
    CONNECT 阶段整体 10s 超时；失败走既有重连/退避语义。
  - **v1 范围：仅 HTTP CONNECT 代理。SOCKS 明确不支持**——配置了 socks 会在
    启动解析期直接报错（而非运行期重连循环反复失败）；对代理本身走 TLS
    （`https://` 代理）与代理认证（userinfo）同样暂不支持，均有明确报错。
  - **作用域：proxy 只作用于「客户端 → server」的 WS 出站**；转发目标
    （target）的流量语义不变（HTTP/TCP/UDP 转发均不经代理）。
- 新模块 `tunely::proxy`：代理 URL 解析 / server URL 目标提取 / CONNECT 请求
  组装与响应解析均为公开纯函数（库用户可复用）；单测锁定字节级格式。
- 集成测试 `tests/proxy_integration.rs`：本机 mock CONNECT 代理全环验证
  （客户端 → 代理 → 服务端 → auth 成功；经代理的 tcp_connect/tcp_data 双向
  回声；代理不可达时快速失败走重连；直连路径不受影响）。

### 不变

- 未配置 proxy 时连接路径逐字节不变（`connect_async` 直连）。
- C ABI（`ffi`）v1 未暴露 proxy 配置（None = 直连），宿主行为不变。
