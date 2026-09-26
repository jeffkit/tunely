# Changelog

本文件记录 tunely（python/ 包）的显著变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.7.0] - 2026-09-26

稳定性/性能批次 P1（协议 wire 格式零变更）：消灭无界缓冲、修复正确性问题、
降低热路径成本。

### Fixed（正确性修复）

- **F8 断连时在途请求悬挂**：隧道 WS 断连注销时，立刻失败该域名的全部在途
  pending——普通请求 future 以 `ConnectionError("tunnel disconnected")` 结束、
  流式请求经 failed 事件立即以错误结束、HTTP 触发的 TCP 转发以错误完成，
  并从注册表清理。服务端监听的真实 TCP 连接（TcpConnectionState）不受影响，
  仍由连接自身生命周期管理。
- **F10 畸形消息不断隧道**：WS 消息循环中非法 JSON / 未知消息类型只丢弃该条
  并告警，不再落入外层 except 导致整条隧道断连注销。
- **F13 流量统计 flush 顺序**：逐域名写库成功后立即推进该域落库快照。
  此前全部写完才推快照，中途异常会整批重发导致重复累加；现在单域失败只影响
  该域（下轮重试），不阻塞其他域名落库。
- **F17 falsy body 丢弃**：`json.dumps(body) if body else None` 类 truthiness
  判断全部改为 `is not None`——请求 body 为 `{}` / `0` / `""` / `False` 时
  不再被静默丢弃（HTTP / SSE 流式 / TCP 三条转发路径及请求日志存储同步修复）。
- **F22 端口冲突友好报错**：TCP 监听端口被占用（EADDRINUSE）时抛出含具体
  `host:port` 与归属隧道的可读错误，不再裸 OSError traceback；其他 OSError
  原样抛出。

### Security（安全加固）

- **L2 请求日志脱敏与截断**：请求日志存储时对 `authorization` / `cookie` /
  `set-cookie` 三个 header（键名大小写不敏感）值替换为 `[REDACTED]`（请求与
  响应 header 均覆盖）；响应 JSON body 双解析归一化路径补 10000 字符截断，
  与既有截断行为一致。

### Changed（变更）

- **F7 SQLite 并发写**：sqlite 方案增加 `connect_args={"timeout": 30}`，
  并在引擎连接上启用 `PRAGMA journal_mode=WAL`（仅 sqlite 方言生效，
  mysql/pg 不受影响）；「请求计数 increment_requests」与「请求日志写入」
  拆成独立事务，日志失败不再回滚计数。
- **forward mode 缓存**：隧道转发模式注册时从 DB 读一次缓存在
  `ActiveConnection.mode` 上，`forward()` 直接用缓存路由，省掉每请求一次
  DB 查询；token 轮换 / 隧道删除后连接仍存活时沿用缓存值。注意：连接存活
  期间修改 mode 需重连后生效。
- **L1 建隧道校验 domain**：`POST /api/tunnels` 校验 domain 匹配既有
  DOMAIN_PATTERN（与 check-availability 同规则：字母/数字开头，可含中划线，
  1-63 字符），不匹配返回 400。既有合法域名（如 dsh / smoke-dom）不受影响。

### Added（新增）

- **内存安全上限（防 OOM）**：
  - `tcp_forward_max_buffer_bytes`（默认 10 MiB，0 = 不限制，env:
    `WS_TUNNEL_TCP_FORWARD_MAX_BUFFER_BYTES`）：HTTP 触发的 TCP 转发单请求
    响应累积缓冲超限时，该请求立即以 "response too large" 失败（502），
    防 /forward 拉大文件打爆内存。
  - `stream_queue_maxsize`（默认 1024，0 = 不限制，env:
    `WS_TUNNEL_STREAM_QUEUE_MAXSIZE`）：SSE 流式单请求数据块队列上界，
    写满即按流错误结束该流（消费侧立刻收到错误 StreamEndMessage），
    不阻塞 WS 消息循环、不无界积压。
- **uvloop 可选加速**：uvicorn 启动时检测 uvloop，可用则以 uvloop 事件循环
  运行，未安装回退默认。新 optional extra `tunely[uvloop]`（不进基础依赖，
  纯性能优化）。

### 升级注意

- 无 schema 变更、无 wire 协议变更，可直接升级。
- 部署了 TCP 监听的实例若与其他进程端口冲突，现在会在启动时收到带
  `host:port` 与归属隧道的明确报错（此前是裸 traceback）。

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
