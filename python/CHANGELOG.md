# Changelog

本文件记录 tunely（python/ 包）的显著变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.6.2] - 2026-09-26

安全审计 P0 修复批次（服务端转发面与连接生命周期）。

### Security（安全加固）

- **路径校验（@-SSRF）**：`forward()` 与 `/api/tunnels/{domain}/forward`
  拒绝非 `/` 开头的 `path`（HTTP 端点直接 400）；`/t/{domain}/` 浏览器路由
  对空/相对路径归一化补 `/` 前缀。此前 `path` 未校验直接拼进客户端的
  `target_url + path`，攻击者可传 `@169.254.169.254/` 改写 host 形成内网 SSRF。
  客户端 SDK 侧同步新增 `normalize_path` 兜底。
- **跨隧道消息归属校验**：`TunnelResponse` / `Stream*` / `Tcp*` 消息路由前
  校验 id/conn_id 的归属隧道与当前 WS 连接一致，不匹配则告警丢弃。此前任一
  token 持有者可伪造他人响应、注入或掐断他人 TCP 流。
- **TCP 出口安全默认值**：`tcp_listen_host` 默认值 `0.0.0.0` → `127.0.0.1`；
  容器/公网部署需显式设 `WS_TUNNEL_TCP_LISTEN_HOST=0.0.0.0`（docker-compose
  示例已同步，并显式启用 `WS_TUNNEL_TCP_MAX_CONNECTIONS=100`）。
- **吊销即断连**：`DELETE /api/tunnels/{domain}` 与
  `POST /api/tunnels/{domain}/regenerate-token` 在 DB 变更成功后立即关闭该
  隧道的存量 WebSocket 连接（reason: `tunnel revoked` / `token rotated`），
  审计 detail 带 `revoked` / `rotated` 标记。此前旧客户端可继续服务。

### Added（新增）

- `tcp_idle_timeout`（默认 300 秒，0 = 不启用，env:
  `WS_TUNNEL_TCP_IDLE_TIMEOUT`）：外部 TCP 连接空闲超时后服务端主动关闭，
  回收「连上不发数据」的慢连接。

### Changed（变更）

- **`max_pending_requests` 正式生效**：forward 转发面的 pending 请求数达到
  上限（默认 1000）后返回 503，不再无限堆积。
- 新增 `forward_max_timeout`（默认 600 秒，0 = 不限制，env:
  `WS_TUNNEL_FORWARD_MAX_TIMEOUT`）：forward 对请求传入的 timeout 做 clamp，
  防止单请求长期占用转发面。

## [0.6.0] - 2026-09-25

安全加固（鉴权/限速/限额）+ 可观测性 + 流量统计持久化 + 管理面审计。

### Breaking（破坏性变更）

- **CORS 默认值由 `*` 收紧为空（仅同源）**。`AppSettings.cors_origins` 与
  `tunely serve --cors-origins` 的默认值改为空字符串：空值时不装配
  `CORSMiddleware`，不输出任何 CORS 头，浏览器跨域请求将被拦截。
  理由：`*` 会把隧道转发面暴露给任意网页来源，属于默认不安全配置。
  **升级影响**：依赖浏览器跨域访问的部署必须显式配置
  `TUNELY_CORS_ORIGINS`（或 `--cors-origins`）为来源白名单或 `*`，
  否则升级后跨域前端将无法调用。

### Security（安全加固）

- 管理 API 密钥比较改用 `hmac.compare_digest`（常数时间，防时序侧信道）。
- WebSocket 认证失败（无效 token / 非认证首消息）断开前延迟 1 秒，
  抑制无限速 token 暴力尝试；业务拒绝路径（disabled / already connected）不受影响。
- 新增每隧道 TCP 并发连接上限 `tcp_max_connections`
  （env: `WS_TUNNEL_TCP_MAX_CONNECTIONS`，0 = 不限制）。
- `tunely serve --api-key` 未显式提供时回退读环境变量
  `WS_TUNNEL_ADMIN_API_KEY`，避免密钥经命令行参数被 `ps` 看到。

### Added（新增）

- **`GET /metrics`**：Prometheus 文本格式指标（无鉴权，勿暴露公网，与 /api 同理）。
  指标：`tunely_tunnels_registered`、`tunely_tunnels_connected`、
  `tunely_tcp_connections_active{domain}`、`tunely_tunnel_bytes_in{domain}`、
  `tunely_tunnel_bytes_out{domain}`。
- **流量统计持久化**：`tunnels` 表新增 `bytes_in` / `bytes_out` 累计列
  （迁移 `004_add_tunnel_bytes`）；服务端启动时从 DB 恢复初值，
  每 30 秒把内存计数增量落库（DB 不可用静默重试），停机前最后落库一次；
  `/api/tunnels` 中的 bytes 统计跨重启连续。
- **管理面审计日志**：新表 `admin_audit_logs`（create_all 自动建表，存量库请跑迁移）；
  埋点覆盖隧道 create / update（记变更字段）/ delete / regenerate-token 及
  WebSocket force 抢占（takeover）；新端点 `GET /api/audit?limit=50`
  （需管理 API 密钥）返回倒序列表，`source_ip` 尽力而为记录。

### 升级注意

- 存量数据库请执行 `alembic upgrade head`（新增 tunnels 两列，无数据变更）。
  全新数据库由 `create_all` 自动建表建列，无需迁移。

## [0.5.0]

多 TCP 监听器（`WS_TUNNEL_TCP_LISTEN`）、每隧道 bytes_in/bytes_out 内存统计、
优雅停机、管理面强制鉴权（配置 admin api-key 后）。
