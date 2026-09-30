# 探测面去特征化（Anti-Probe Hardening）

> 状态：**T1–T7 已落地（服务端）；T8 为运维操作，待执行**。发布版本 bump（py 0.12.0）随 release 进行（仓纪律：实现期间不 bump）。
> 范围：公网可见的 HTTP/WS 探测面收敛；**不改 wire 协议消息类型**，三端客户端零改动
> 背景：wss 部署下被动流量分析只见 SNI/流量形态，协议内容不可见；真正一戳就暴露的是主动探测面。本文只解决主动探测。
> 关联：docs/MIGRATION_TCP_ONLY.md（0.11 原生 TLS）、docs/ROLLING_UPGRADE.md（发布纪律）

## 0. 探测面现状清单

| # | 探测点 | 现状 | 位置 | 指纹强度 |
|---|--------|------|------|----------|
| P1 | `GET /` | 返回 `{"service":"Tunely Server","version","domain","status":"running"}` | `app.py` root 路由 | 强 |
| P2 | `GET /health` | 泄露 `connected_tunnels` 活跃数（活动 oracle） | `app.py` | 中 |
| P3 | `GET /api/info` | name/version/domain 规则/ws url，**无 admin 门控** | `server.py` | 强 |
| P4 | `GET /metrics` | registered 隧道数 + 会话统计，**无门控** | `server.py` | 中 |
| P5 | `/docs` `/redoc` `/openapi.json` | FastAPI 默认全开，title "Tunely Server" + 全路由表 | `app.py`（portal 已关，主 app 没关） | 强 |
| P6 | WS 认证失败路径 | auth_error JSON 文案区分 `Invalid token`/`Tunnel is disabled`/`connection_exists`，延迟不一致（无效=1s、禁用/已在线=0s） | `server.py _handle_websocket` | **强（token 枚举 oracle）** |
| P7 | `auth_ok.server_version` | 恒发真实版本 | `server.py` | 中 |
| P8 | 主域名 catch-all 404 | `{"detail":"Not Found"}`（通用 FastAPI 风格） | `app.py` | 弱 |
| P9 | 部署层 | uvicorn `Server` header；portal/downloads/隧道入口同域混布；`ws_path` 固定 `/ws/tunnel` | `deploy/` | 中 |
| P10 | 原生 TLS ALPN | 0.11 锁 `http/1.1`（真实站点普遍 h2+http/1.1 并存） | 0.11 TLS 监听 | 弱 |

**P6 的关键取舍（必须写进实现者的脑子）**：现状是故意的——`tests/test_server_hardening.py` 的
`test_disabled_tunnel_no_delay` / `test_already_connected_no_delay` 明确锚定「业务拒绝不加延迟」。
但「无效 token = 慢、有效 token（哪怕禁用/在线）= 快」恰好构成 **token 存在性与在线状态 oracle**：
拿 token 字典打 `/ws/tunnel`，快响应 = 这个 token 是真的。本 spec 推翻该取舍：对探测者行为统一，
对合法客户端无感（已核实：py/ts/rust 三端客户端收到 auth_error 后只展示 `error` 文案，
**不分支消费 `code`**，TS 里唯一的 `.code` 分支是本地 socket 的 ECONNREFUSED）。

## 1. 原则

1. **wire 协议零破坏**：`auth_error` / `auth_ok` 消息类型与字段全部保留，只改服务端行为与取值。
2. **可配置回退**：去指纹行为默认开启，部署特例可关。
3. **服务端先行**：所有改动只动 py 服务端；客户端 / admin-console 零改动（同 ROLLING_UPGRADE 纪律）。
4. **测试锚定同步改写**：P6 相关既有用例按新语义改写（不是删除）。

## 2. 任务清单

### T1 关闭 FastAPI 自暴露（P5）— `app.py`

- `create_full_app` 的 `FastAPI(...)` 加 `docs_url=None, redoc_url=None, openapi_url=None`
  （与 `deploy/portal/app.py` 同做法）；模块级默认实例 `app = FastAPI(title="Tunely Server", ...)` 一并处理。
- `title` / `description` 去掉 "Tunely" 字样 → `title="API Server"`。
- 验收：`GET /docs|/redoc|/openapi.json` → 404；全站响应无 "Tunely" 字样。

### T2 `GET /` 去指纹（P1）— `app.py` root 路由

- 子域名转发分支**保留不动**；主域名分支删除 service/version/domain JSON，改为：
  - 新 config `TUNELY_ROOT_RESPONSE_FILE: str = ""`：非空时返回该文件内容（`text/html`），
    给部署一个挂 decoy 落地页的口子；
  - 空（默认）→ `PlainTextResponse("OK")`。
- 验收：主域名 `GET /` 不含 tunely/version/domain 字样；现有锚定 root JSON 的用例更新。

### T3 `/health` 收敛（P2）— `app.py`

- 返回 `{"status":"ok"}`，去掉 `connected_tunnels`。
- 活跃数需求走既有 admin 门控的隧道列表接口（本任务不改它）。
- 验收：`GET /health` 无隧道数；用例更新。

### T4 `/api/info` + `/metrics` admin 门控（P3/P4）— `server.py`

- 两端点加与既有 admin 端点一致的 key 校验（复用 `_check_admin_api_key`；
  取 key 方式同构：`x-api-key` Header）。
- ⚠️ **语义边界（实现时确认）**：`_check_admin_api_key` 在**未配置** `admin_api_key` 时恒放行
  （与既有 admin 端点语义一致）。即默认部署下这两个端点仍开放——**生产部署务必配置
  `WS_TUNNEL_ADMIN_API_KEY`**，配置后无/错 key 一律 401。
- 已核实仓内无 consumer（admin-console / cli / ts / portal 均不调用这两个端点）；
  外部监控需带 key——**breaking，版本按 minor 走**。
- 验收：无 key → 401；带正确 key → 响应与现状逐字节一致。

### T5 认证失败路径统一化（P6，本 spec 核心）— `server.py _handle_websocket`

统一所有「认证类失败」为同一时序 + 同一文案：

- 覆盖分支：非 auth 首包 / token 不存在 / tunnel disabled / 已在线拒绝（register 失败）。
  DB 未初始化（close 1011）保留原样——服务端故障类，罕见且不携带 token 信息。
- 行为：`AuthErrorMessage(error="Authentication failed", code="auth_failed")`
  → `await asyncio.sleep(random.uniform(0.8, 1.6))` → `close(1008)`。
  不再区分 `Invalid token` / `Tunnel is disabled` / `connection_exists`。
- 常量：`_AUTH_FAILURE_DELAY` 改为模块级区间常量（保持可测）。
- 测试改写：`test_invalid_token_sleeps_before_close` 保留（断言改区间）；
  `test_disabled_tunnel_no_delay` / `test_already_connected_no_delay` 反转为
  「同样有延迟 + 同一文案」；文案断言 `"Invalid token"` → `"Authentication failed"`。
- 客户端影响：零（已核实，见 §0）。

### T6 `auth_ok.server_version` 可关（P7）— `server.py` + `config.py`

- 新 config `WS_TUNNEL_EXPOSE_VERSION: bool = False`：False 时 auth_ok `server_version=""`。
- 字段类型不动（py `str` Field、ts `server_version?: string`、rust `Option<String>`——
  空串解析无碍；已核实 py client 不消费该字段）。
- `/api/info`（T4 后已门控）内部仍回真实版本，不受此开关影响。
- 验收：默认连接 `auth_ok.server_version == ""`；开关打开回真实版本；
  实现时核对 `spec/conformance/wire.json` 是否锚定该字段，锚定则同步。

### T7 文档同步

- `docs/PROTOCOL.md`：auth_error 小节加注「服务端可能对全部认证类失败统一返回 `auth_failed`
  并延迟关闭（探测去特征化，见 docs/PROBE_HARDENING.md）」；
  `auth_ok.server_version` 加注「可配置为空串」。
- 本文档落库；`CLAUDE.md` 版本号校正（写的是 0.10.0，实际已发 0.11.0）。

### T8 部署层（不进 SDK，落 `deploy/README.md` + 运维操作单）

- nginx：`server_tokens off` + `proxy_hide_header Server`（或覆写为 `Server: nginx`）；
  只反代精确路径（`location = <ws_path>`、`/t/`、`/api/`），其余 fallback 到 decoy 静态站；
- `ws_path` 随机化：config 已支持，部署指南加随机路径生成示例；
- portal / downloads 与隧道入口**拆域名**（`deploy/install.sh` 的 `BASE` 域名与 wss 域名分离）；
- 原生 TLS 部署（0.11）：ALPN 仅 `http/1.1` 的取舍记录在案（放开 h2 属后续项）。

## 3. 不在本期（记录在案）

- 心跳 jitter（固定 30s ping 的时序形态）：要动客户端+服务端，TLS 下仅剩时序弱特征，收益低；
- WS 探测 decoy（对未认证连接伪装通用服务）：REALITY 级工程，超出「自托管隧道」定位；
- 客户端 TLS 指纹仿真（uTLS 类）：同上。

## 4. 兼容性与验收

- wire：消息类型/字段零变化；仅取值与文案变化 → 三端零改动；
- breaking：`/api/info`、`/metrics` 变 401（**仅当部署配置了 admin_api_key**；未配置时放行，
  见 T4 语义边界）；`/health`、`GET /` 响应形状变化（均无仓内 consumer）；
- 发布：py minor（0.12.0）单 PR（T1–T6 + T7 文档），服务端先行零风险；T8 运维操作单独执行；
- **外部验收清单（探测复测）**：对公网入口依次
  `curl -s /`、`/health`、`/api/info`、`/metrics`、`/openapi.json`、`/docs`、
  无 token WS、错 token WS、禁用 token WS、非 auth 首包 WS——
  全部响应无 tunely/version/domain 字样，且各失败路径时序不可区分。

## 5. 落地记录（T1–T7）

- **T1**：`app.py` 两处 `FastAPI(...)` 均关闭 `docs/redoc/openapi`，title 改 `API Server`
  （随 T1 移除了 app.py 里失效的 `_pkg_version` 副本）。
- **T2**：root 主域名分支改 `PlainTextResponse("OK")`；新 `AppSettings.root_response_file`
  （env `TUNELY_ROOT_RESPONSE_FILE`）支持 decoy 落地页；子域名转发分支不变。
- **T3**：`/health` → `{"status": "ok"}`。
- **T4**：`/api/info`、`/metrics` 加 `x-api-key` Header 校验（`_check_admin_api_key`，
  语义边界见上文）。
- **T5**：`server.py` 新增 `_AUTH_FAILURE_DELAY_RANGE = (0.8, 1.6)`、`_AUTH_FAILURE_MESSAGE`
  与统一出口 `_reject_auth()`；四个认证失败分支（非 auth 首包 / token 不存在 / disabled /
  已在线拒绝）全部改走该出口。
- **T6**：`TunnelServerConfig.expose_version`（env `WS_TUNNEL_EXPOSE_VERSION`，默认 False），
  auth_ok 据此回真实版本或空串；`/api/info` 不受该开关影响（门控后属运维面）。
- **T7**：PROTOCOL.md（auth_error 统一注记 + server_version 注记）、CLAUDE.md
  （Error Handling 对齐 + 版本号校正 0.10.0 → 0.11.1）。
- **测试**：`test_server_hardening.py::TestAuthFailureDelay` 按统一语义改写
  （disabled/已在线从「不加延迟」反转为「同延迟同文案」）；
  `test_server_072_version.py` 拆为默认空串 + env 开启两用例；
  新增 `test_probe_hardening.py`（T1–T4）；
  `test_server_observability.py` 的 `test_metrics_no_auth_required` 更名为
  `test_metrics_open_when_key_unconfigured` 对齐新语义。
  全量 pytest 通过（451+ 用例）。ruff 本机不可用（pyenv shim 缺失），语法/导入由测试覆盖。
