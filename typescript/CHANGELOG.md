# Changelog

本文件记录 tunely（typescript/ 包，npm: tunely）的显著变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.5.0] - 2026-10-12

### Added

- **客户端出站支持 HTTP CONNECT 代理**（客户端 → server 的 WS 连接可经代理转发，
  跨境直连受限场景下可切更快的代理线路；与 rust 客户端 0.6.0 同规格）：
  - 配置面：`TunnelClientConfig.proxy`（SDK）；配置文件 `proxy = "http://host:port"`
    （`client.toml` 顶层字段，多隧道形态下全局共享）；环境变量回退
    `HTTPS_PROXY` / `https_proxy` / `ALL_PROXY` / `all_proxy`
    （wss:// 语义按 https 处理）；**优先级：配置 > env > 无**
    （注意与 TUNELY_* 的 CLI > env > file 顺序不同：站点级配置覆盖部署环境
    通用变量）。v1 不设 CLI 旗标。
  - 实现：新增依赖 `https-proxy-agent`——它与代理建 CONNECT 隧道后把隧道
    socket 交给 ws 库，对 ws:// 与 wss:// 目标均适用（wss 的 TLS 在隧道**内部**
    由 ws 的 https 模块完成，端到端加密不变）；选它而非 proxy-agent/global-agent：
    轻量零子依赖、仅做隧道建立、语义可预期。非法配置（SOCKS 等）在构造/
    解析期 fail fast，不进重连循环。
  - **v1 范围：仅 HTTP CONNECT 代理。SOCKS 明确不支持**；对代理本身走 TLS
    （`https://` 代理）与代理认证（userinfo）同样暂不支持，均有明确报错。
  - **作用域：proxy 只作用于「客户端 → server」的 WS 出站**；转发目标
    （target）的流量语义不变（HTTP/TCP 转发均不经代理）。
- 新模块 `src/proxy.ts`：`parseProxyUrl` / `validateProxy` / `proxyFromEnv` /
  `resolveProxy` / `serverHostPort` 公开导出（SDK 用户可复用），配置面与
  rust/src/proxy.rs、rust/src/config.rs 一致并有跨端键序锁定测试。
- 集成测试 `src/proxy.integration.test.ts`：本机 mock CONNECT 代理（Node http
  server 的 `connect` 事件）+ 真实 ws 服务端全环验证（客户端 → 代理 → 服务端
  → auth 成功；经代理的 tcp_connect/tcp_data 双向回声；代理只被连一次）。

### 不变

- 未配置 proxy 时连接路径行为不变（WebSocket 选项保持 `{ perMessageDeflate: true }`）。
- CLI 无新增旗标；`connect` 的输出仅在配置了代理时多一行「代理: …」。
