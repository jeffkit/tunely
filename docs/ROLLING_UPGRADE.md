# Tunely 升级方案：服务器 × 客户端协同更新（最小打扰）

> 状态：v2 草案——经三路评审修订（协议兼容 / 运维 SRE / 务实规模），P0 问题已吸收。
> 目标一句话：**服务端与三端客户端的每次升级，正在用隧道的人只经历「一次秒级失败 + 自动恢复」，且升级本身可核对、可回滚。**
> 刻意不承诺「绝对零中断」：单服务器进程下 WS 隧道在服务端升级时必然重连（§3 讲清真实影响面）。
> 关联：协议 v2（issue #3/#4）、动态监听器 API（0.8.0）

## 1. 验收定义（A1–A4）

| # | 验收项 | 指标 |
|---|--------|------|
| A1 | 在途请求不悬挂 | 断连时在途请求**立即失败**（0.7.0 F8 已实现），不超时挂死、无半写 |
| A2 | 断连窗口秒级 | 客户端自动重连 ≤ 重连间隔（默认 5s）；窗口内新请求拿到**快速明确错误**（进程活着=503，进程换了=nginx 502），都可立即重试；不做 nginx 层 502→503 归一（一段 `error_page` 配置可选项见 §6 R7） |
| A3 | 滚动期任意组合互通 | 新服务端 × 旧客户端、旧服务端 × 新客户端全部可用（规则见 §5，矩阵见 §6） |
| A4 | 单命令回滚 | 回退上一版本不动 DB、不动配置，5 分钟内完成（回滚源必须是 git ref，不是 PyPI，见 §4.3） |

## 2. 现状与地基

**拓扑**：crypto VPS systemd 跑 tunely-server（nginx 反代，3 条隧道 dsh/gz/plaita）；Mac launchd 跑 TS 客户端 ×2；tcloud_gz systemd 跑 rust 客户端。维护者一人，升级频率 ≤ 每月一次。

**已核实、可依赖的地基**（评审逐条验证过代码）：

- 三端消息解析均容忍未知字段/类型：pydantic 默认 ignore extra（protocol.py 无 model_config 限制）；TS interface 可选字段 + 未知 type 走 default 分支只告警；rust serde 无 `deny_unknown_fields`，未知 type 表现为整条消息解析失败→告警丢弃（等效"忽略"，注意：是"看不见"而非"看见了不处理"，见 §5 规则脚注）。
- 客户端自动重连：TS/rust 5s 无限重连 + 25s/45s keepalive 判死；py 5s 重连 + websockets 库级 ping（30s/10s）。
- F8：隧道断连在途 pending 立即失败（A1 服务器侧语义已闭环；"上层会重试"是调用方属性，agent 工具链会、人不会）。
- 服务端对未知/畸形 WS 消息只告警不断连（F10，server.py WS 循环 try/except per message）。

**已核实的缺口**：

- G1 `server_version` 谎报：服务端发 AuthOk 从未传真实版本，永远发默认值 `"0.1.0"`（server.py 唯一构造点只传 domain/tunnel_id）。
- G2 **三端 `client_version` 假值**（评审 P0，原方案遗漏）：rust 发真实版本；**TS `createAuthMessage` 硬编码 `'0.1.0'`；py 客户端不传字段、由 pydantic 默认补 `"0.1.0"`**。服务端还收了不用（不记录、不上报）。→ 一切"按版本核对现网/做门禁"的想法，在修掉这个之前都是空中楼阁。
- G3 无优雅退出：全仓无任何 signal 处理，SIGTERM 由 uvicorn 接管直接停——在途请求随进程死（nginx 502）。
- G4 无版本门禁与能力协商（当前**不需要**，见 §4.4 推迟项）。
- G5 无升级编排（用 §4.1 的手工清单代替，脚本化推迟）。

## 3. 今天 restart 的真实影响面（诚实版）

以 0.7.1 现状执行 `systemctl restart tunely-server`：

1. SIGTERM → uvicorn 停收新连接 → 3 条隧道 WS 全部断开；
2. 在途请求：经 F8 **立即失败**（无悬挂）——调用方（agent 工具链/浏览器）拿到一次明确错误；
3. 窗口内新请求：进程活着时 503 "Tunnel not connected"（注意：`/forward` API 是 HTTP 200 包 `body.status=503`，见 §4.3 判据说明）；进程死后 nginx 502；
4. 服务端 `RestartSec` + 客户端 5s 重连 → **最坏 ~8s 全量自愈**；
5. 净效果：低峰执行 = 一个人可能看到 1–2 条报错，重试即恢复，无数据损坏。

这就是"最小打扰"的物理下限，也是本方案所有"推迟项"的衡量基准：任何复杂度投入，必须能把这条基线再往下压才有意义。

## 4. 分级方案

### 4.1 第 0 级：升级纪律（纯文档，今天生效）

0.7.1 手工升级清单（现网 unit 名/端口/路径以 `systemctl cat tunely-server` 为准，下述为占位）：

```bash
# 0) 预检：记录基线（各隧道 connected 现状 + 当前版本 + 安装来源 commit）
curl -sf 127.0.0.1:<PORT>/api/tunnels -H "x-api-key: $WS_TUNNEL_ADMIN_API_KEY" | jq '.[]|{domain,connected}'
#    admin key 取自 /etc/tunely/env 的 WS_TUNNEL_ADMIN_API_KEY；/metrics 勿走公网域名

# 1) 备份 DB（WAL 模式下 .backup 在线安全；路径从 WorkingDirectory 解析，勿照抄）
sqlite3 /var/lib/tunely/data/tunnels.db ".backup /var/backups/tunnels-$(date +%s).db"

# 2) 部署 + 重启（tag 或 commit 二选一，见 §4.3 安装源规则）
uv tool install --force "tunely @ git+https://github.com/jeffkit/tunely@v0.7.1#subdirectory=python"
systemctl restart tunely-server        # 无 drain，接受 ~8s 窗口（§3）

# 3) 健康门（人工，3 分钟内逐项）：
curl -sf 127.0.0.1:<PORT>/metrics | grep tunely_tunnels_connected
curl -sf 127.0.0.1:<PORT>/api/tunnels -H "x-api-key: $KEY" | jq '.[]|{domain,connected}'
#    探测（判据见 §4.3：必须校验 body.status，HTTP 200 不代表通）
curl -s 127.0.0.1:<PORT>/api/tunnels/dsh/forward -H "x-api-key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"method":"HEAD","path":"/","timeout":15}' | jq '.status'   # 期望 200（任意 2xx/3xx 亦可）

# 4) 回滚（只在健康门不过时；源是 git ref，不是 PyPI）
uv tool install --force "tunely @ git+https://github.com/jeffkit/tunely@<prev-ref>#subdirectory=python"
systemctl restart tunely-server
```

纪律：升级挑近 5 分钟无流量的时刻（看 `/metrics` 速率或 console）；执行时间写进 commit/运维记录；§3 的窗口是预期行为，不是事故。

### 4.2 第 1 级：真实版本上报（0.7.2，三端小改，~60 行）

修复 G1+G2，让"升级核对"从人工记忆变成可查询。这是"服务器和客户端同时更新"的第一个最小实践——按 §5 规则，服务端先行（加可选字段），客户端随后：

1. **服务端**：AuthOk 传真实 `server_version`（`importlib.metadata.version("tunely")`，与 `/api/info` 同源）；`ActiveConnection` 记录 `client_version`，`/api/tunnels` 的 `TunnelInfo` 暴露（缺省 `"unknown"`）。
2. **TS**：`createAuthMessage` 改读 package.json 版本（当前硬编码 `'0.1.0'`）。
3. **py**：客户端 AuthMessage 显式传 `tunely.__version__`。
4. **rust**：顺手给 `AuthMessage.client_version` 加 `#[serde(default)]`（当前是必填 String，与 TS/py 可选语义不对称——唯一会把"加可选字段"变成解析事故的写法，§5 脚注）。
5. **测试**：TS 端 `protocol.conformance.test.ts` 对 `createAuthMessage` 输出做**精确键集断言**，加字段必红，需同步更新；wire.json（py/rust）是子集断言，不受影响。

### 4.3 安装源与判据规则（评审抓出的三个必翻车点）

- **tag 纪律**：仓库目前只有 `python-v0.5.0` 一个 tag。发布即打 tag（沿用 `python-vX.Y.Z` 或统一定 `vX.Y.Z`），安装/回滚一律 `git+https://github.com/jeffkit/tunely@<ref>#subdirectory=python`——**仓库根没有 pyproject.toml**（在 `python/` 子目录），不带 `#subdirectory` 无法构建。
- **PyPI 不可作回滚源**：PyPI 上 tunely 停在 0.4.1（2026-06），0.5+ 从未发布。`uv tool install tunely==0.7.0` 会装到 0.4.1 或直接失败。
- **健康门判据**：`/api/tunnels/{d}/forward` 路由今天返回 **HTTP 200 + body 里 `status=503`**（`response_model=ForwardResponse`，状态码在 body 内）。所以探测判据必须是 **HTTP 200 且 `body.status` 为 2xx/3xx**；只看 HTTP 状态码的健康门永远绿灯。未来若做 drain，503+`Retry-After` 必须在**路由层**用 `HTTPException(status_code=503, headers={"Retry-After": "10"})` 发（body 包裹路径带不上真头）；浏览器路径 `/t/{d}/`（app.py）已把状态映射到 HTTP 层，可作为探测替代。

### 4.4 推迟项（每项挂明确再触发条件，防过度设计）

| 推迟项 | 消除的痛点 | 再触发条件 | 备注（实现要点已存档） |
|--------|-----------|-----------|----------------------|
| capabilities 协商 | 协议 v2 新旧并存 | 协议 v2（binary_frames/chunked_http，issue #3/#4）**动工当天** | 约定"字段缺失=空集合"并写入 PROTOCOL.md；服务端只回交集 |
| min_client_version 门禁 | 客户端过旧无限空转 | 任何客户端**脱离维护者控制**（他人接管/不受升级节奏管） | 依赖 0.7.2 的真实版本上报；比较语义=按 `.` 分段数值比较（禁字典序），不可解析值 fail-open+告警；注意存量 TS/py 客户端永远报 0.1.0，门禁对其不可区分，只能人工升级兜底 |
| drain-then-restart | 重启砍掉在途请求 | 升级**无人值守**、出现第二个用户、或多分钟长任务成常态 | 实现要点见附录 A（uvicorn 有三个坑，评审已探明） |
| upgrade.sh + 自动回滚 | 手工步骤/脚本腐烂 | 升级频率 > 每月一次，或需要无交互 shell 升级 | §4.1 清单即脚本骨架；自动回滚需防瞬态误触发（基线 connected、失败重试 2 次） |

被否决方案备忘——**蓝绿双实例不做**：蓝绿只能消除 HTTP 管理面重启窗口；WS 隧道连的是进程内连接，换进程必重连，数据面断连窗口省不掉，只省 console 几秒 502，不值双实例常驻复杂度。

## 5. 兼容规则（谁可以单边先行）

| 改动类型 | 服务端先行 | 客户端先行 | 依据/脚注 |
|----------|:---:|:---:|-----------|
| 消息加**可选**字段 | ✅ | ✅ | 三端解析器均忽略未知字段；**rust 脚注**：给"收到的"消息加字段必须 `#[serde(default)]`（照 protocol.rs server_version 既有模式），否则旧对端不发该字段时 tagged enum 整条解析失败 |
| 新消息类型 | ⚠️ 仅能力门控后 | ⚠️ 同左 | rust 端未知 type = 整条消息静默丢弃——是**可达性**问题（旧端"看不见"），不只是语义问题 |
| 字段默认值/语义变化 | ❌ | ❌ | 新语义必须走新字段，旧字段冻结；rust 对已知字段类型错误同样整条丢弃，类型变更比语义变更更危险 |
| 删除/改名既有字段 | ❌ | ❌ | 永久禁止（或双写过渡一个周期） |
| 新配置项/新端点 | ✅ | ✅ | 客户端不感知 |

升级顺序纪律：**服务端先行**（新服务端兼容现网全部客户端版本，升前核对 §6 矩阵）；客户端随意跟（新客户端必须兼容当前生产服务端）；危险改动走能力门控，新旧路径并存至少一个升级周期。

## 6. 兼容矩阵

| 服务端 \ 客户端 | TS ≥0.2.5 | TS <0.2.5 | rust ≥0.1.1 | rust <0.1.1 | py ≥0.6.2 |
|----------------|:---:|:---:|:---:|:---:|:---:|
| ≥0.7.0 | ✅ | ⚠️ 静默死连感知缺失¹ | ✅ | ⚠️ 同左 | ✅ |
| 0.7.1 | ✅ | ⚠️ | ✅ | ⚠️ | ✅ |

> ¹ keepalive 在 TS 0.2.5 才发布（da49a69 落地、a555e49 bump 版本）。旧 TS **没有**看门狗，不会"重连风暴"——真实缺陷是空闲连接被中间设备掐断后无法感知，只能靠服务端 uvicorn ws ping（30s/10s）部分缓解。"重连风暴"发生在相反组合（新客户端 × 不回 Pong 的旧服务端，0.6.1 回归，已修），不在本矩阵行列内。
>
> 现网实际组合全部为 ✅（待 0.7.2 上线后可用 `/api/tunnels` 的 `client_version` 直接验证——此前该数据不可信，见 G2）。

## 7. 风险清单

- **R1** drain 未实现的窗口：见 §3，可接受；nginx 可选配 `error_page 502 = @unavail; return 503 + Retry-After` 把进程死亡窗口的错误归一成可重试语义（R7）。
- **R2** SSE 长流被重启切断：以错误/断连结束，agent 工具链会重试，人不会——0.7.2 起失败原因在日志可查。
- **R3** 版本门禁误伤：已推迟（§4.4），启用前提是 0.7.2 已上线且 `/api/tunnels` 版本分布核对过。
- **R4** 合成探测打到真实后端：HEAD / 无副作用；计入请求日志（后台队列，无热路径代价）。
- **R5** DB 备份与迁移：`.backup` 在 WAL 下在线安全（database.py 已确认 WAL）；运行时只做 `create_all` **从不自动跑 alembic**——0.8.0 若带加列迁移，升级清单必须显式加 `alembic upgrade head` 并记录 revision；0.7.1 无迁移，回滚 schema 无忧。
- **R6** drain 类改动（推迟项）期间客户端恰好重连：现有三端把 AuthError 当可重试错误继续重连，无需改动；实现时"drain 期间新注册拒绝或接受"二选一写死即可。
- **R7** 备份路径三种写法并存（config.py `./data/tunnels.db`、cli 默认 `tunely.db`、unit 模板 `/var/lib/tunely/data/tunely.db`）：以 `systemctl show tunely-server -p WorkingDirectory` 解析为准，备份目录预先 `install -d`。

## 8. 验收清单

**0.7.2（第 1 级）**
- [ ] AuthOk 携带真实 server_version；`/api/tunnels` 暴露各隧道 client_version
- [ ] TS/py 客户端上报真实版本；rust `client_version` 加 `#[serde(default)]`
- [ ] TS conformance 精确键集断言同步更新；三端测试全绿
- [ ] 真机核对：升完后 `/api/tunnels` 三条隧道版本号=最新（这是本方案第一次可查询的"同时更新"验证）

**推迟项动工时**
- [ ] capabilities："缺失=空集合"写入 PROTOCOL.md；服务端只回交集
- [ ] 门禁：分段数值比较 + fail-open；存量假值客户端的兜底说明
- [ ] drain：附录 A 三个 uvicorn 坑逐一验证；`TimeoutStopSec` 与 `timeout_graceful_shutdown` 联动
- [ ] 回滚演练：手动执行一次 v<prev> 回退（A4 目前无验证步骤）

## 附录 A：drain-then-restart 实现要点（评审探明，避免重踩）

1. **SIGTERM 处理器不能在 serve 入口注册**：uvicorn `Server.serve()` 第一步 `capture_signals()` 会覆盖之前设置的一切 handler。正确姿势：在 **lifespan startup 里**（晚于 capture_signals）重新 `signal.signal(SIGTERM, ...)`。
2. **等待逻辑不能放 lifespan shutdown**：uvicorn 的 `shutdown()` 先关 listener、再关全部连接（含隧道 WS，F8 随即失败），之后才跑 lifespan 收尾——放那里 drain 是空转。
3. **必须自持 `uvicorn.Server` 引用**：`uvicorn.run()` 不返回实例；要手工 `Server(Config(...))`，drain 完成后设 `should_exit = True`。配套 `timeout_graceful_shutdown=30`（否则压线进入的 600s LLM 请求会把退出拖到 `TimeoutStopSec` 被 SIGKILL，优雅退出被兜底击穿）。
4. 计数器要新增：`pending_requests_count()` 之外补 stream/tcp 两个计数方法；`_tcp_connections`（真实外部 TCP，空闲超时默认 300s）有意不计入——长空闲连接（如 ssh-over-tunnel）会被硬切，runbook 需提示。
5. `TimeoutStopSec` ≥ drain_timeout(30) + graceful(30) + 收尾（log worker 5s + TCP reaper 5s + flush）→ 建议 90–120。

## 附录 B：0.7.1 已就绪状态

- 0.7.1（34c9948）纯服务端内部改造：无协议改动、无 DB 迁移、客户端零兼容问题；唯一行为变化是 HTTP 响应体 >100MB 返回 502（`WS_TUNNEL_HTTP_MAX_RESPONSE_BYTES` 可放开），现网 LLM 流量远低于此。
- 升级观察项：`/metrics` 的 `tunely_request_logs_queued`（常态 0/个位数）与 `tunely_request_logs_dropped_total`（应为 0）。
