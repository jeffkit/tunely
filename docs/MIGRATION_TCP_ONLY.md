# 数据面收敛设计：退役 HTTP 模式（TCP-only）

> 状态：v1（评审驱动设计稿，待 jeffkit 定稿）
> 动机：2026-09 深度 review 实测确认——HTTP 模式（JSON 控制面承载 body）的缓冲路径对 JSON/二进制/gzip 响应**整类损坏**（200 + content-length 失配 → 连接掐断 / gzip 解压后仍透传 content-encoding）。TCP 模式（binary 帧）无此类问题。
> 原则：**沿用协议 v2 纪律——服务端先行全兼容旧客户端；一切移除经 deprecation 门控；控制面 auth/tcp/udp/ping 不动。**

## 0. TL;DR

隧道数据面收敛为 **TCP 字节管道（binary 帧）+ UDP** 单一形态。HTTP 语义不消失，而是**归位到边缘 nginx**：nginx 说 HTTP，tunely 说字节。协议层删除 `request/response/stream_*` 消息族与 `chunked_http` 能力；服务端删除 `/t/`、`/forward`、`forward_stream` 及全部流式队列机制；三客户端删除 HTTP 执行路径（rust 本就没有，改动最小）。

- 版本节奏：**0.11.0 = deprecation + 原生 TLS**（路由保留、能力摘除、`wss://` 与监听 TLS 自持、文档改口）→ **1.0.0 = removal**（代码/消息/路由删除）。
- 兼容性关键事实：`TunnelRequest` 是 **server→client** 方向——新服务端不再发送它，旧客户端只会闲着而不会崩。唯一硬破坏点是 `/t/` 路由消失，因此它留到 1.0。

## 1. 背景与动机

review 实测结论（详见评审记录，环境：真实 server+client+目标服务）：

| 场景（经 `/t/` HTTP 模式） | 结果 |
|---|---|
| JSON 响应 | 200 + 旧 content-length，**实际 0 字节，连接被 h11 掐断** |
| 二进制响应 | 同上，0 字节 |
| gzip 响应 | body 被解压成明文，但透传 `content-encoding: gzip` + 旧 content-length |
| 纯文本 | 唯一幸存路径 |

根因是结构性的：body 以 JSON 字符串形态走 WS 控制面（客户端 `errors="replace"` 有损解码、服务端 `json.loads→json.dumps` 重序列化、内层 hop-by-hop 头透传）。**修复它 = 在隧道里重新发明一个（坏掉的）nginx；而生产链路里 nginx 本来就站在那个位置**（docker-compose.yml 已注释「9080: TCP 出口（映射给 nginx 转发）」，`WS_TUNNEL_TCP_LISTEN: "9080:dsh"` 在跑）。

TCP 模式白送的能力：SSE 真流式、chunked 上传、half-close、嵌套 WebSocket、任意二进制协议。三端客户端均已实现 TCP 模式（TS 有 `handleTcpConnect/Data/Close`；rust 是参考实现）。

## 2. 目标架构

```
                         ┌──────────────────────── 公网 ────────────────────────┐
浏览器/CLI ── HTTPS 443 ─▶│ nginx                                                │
                          │  ├─ stream(443): SNI 分流 ──▶ 127.0.0.1:9080 (dsh)  ──┤
                          │  │                            127.0.0.1:9081 (foo)   │
                          │  └─ http(8084): 主站/portal/console/downloads        │
                          │       └─ /ws/tunnel ──▶ 127.0.0.1:8000 (tunely)      │
                          └──────────────────────────────────────────────────────┘
                                                        │ WS 控制面（auth/tcp_*/udp_*/ping）
                                                        ▼
                                              内网客户端 ──▶ 127.0.0.1:8080 目标服务
```

职责划分（写进 README 的第一张图）：

- **nginx**：TLS 终止、L7 路由（子域名/路径）、SSE/上传语义、访问日志、每请求限流/鉴权（portal auth_request）。
- **tunely**：端口编排（`WS_TUNNEL_TCP_LISTEN`/`WS_TUNNEL_UDP_LISTEN`）、隧道注册与鉴权（token）、字节/数据报搬运、管理 API。
- **客户端**：连接保持、重连、把 `tcp_open` 落到本地 `host:port`。

> **nginx 不是必需品**：它只是发布形态之一。0.11 原生 TLS 落地后，单隧道/临时部署用形态 C'（零边缘直发）；「单 443 承载多域名」的 SNI 分流才是必须借 nginx/Caddy 之处（§7）。

## 3. 兼容性铁律

1. **服务端先行**：任何版本的新服务端必须完整服务旧客户端（0.7.x 起），沿用 ROLLING_UPGRADE 纪律。
2. 方向性安全：`TunnelRequest/Response/Stream*` 是 server→client/client→server 的**请求执行对**。服务端停发 `TunnelRequest` 后，旧客户端无感知（只是收不到请求），不会崩。
3. 唯一硬破坏 = `/t/{domain}` 与 `/api/tunnels/{domain}/forward` 路由消失 → 放在 1.0，deprecation 窗口 ≥1 个 minor。
4. 能力协商门控一切新旧行为：服务端从 `SERVER_CAPABILITIES` 摘除 `chunked_http` 后，旧客户端即便声明也协商为空集，行为自动回退，无需客户端先行升级。

## 4. 协议清理清单（wire 层）

### 4.1 消息

| 消息 | 方向 | 处置 |
|---|---|---|
| `TunnelRequest`（含 `stream_ok`） | server→client | 0.11 停发；1.0 删模型。1.0 后收到按 F10 丢弃 + warning「客户端/服务端版本过旧」 |
| `TunnelResponse` | client→server | 同上 |
| `StreamStartMessage` / `StreamChunkMessage`（含 `encoding`）/ `StreamEndMessage` | client→server | 同上 |
| `AuthMessage` / `AuthOkMessage` / `AuthErrorMessage` | 双向 | **不动**（`capabilities` 协商保留） |
| `TcpConnectMessage` / `TcpDataMessage` / `TcpCloseMessage` | 双向 | **不动**（主数据面） |
| `UdpOpenMessage` / `UdpCloseMessage` | 双向 | **不动** |
| `PingMessage` / `PongMessage` | 双向 | **不动**（并在 0.11 补齐服务端主动 ping，见 §11） |

### 4.2 能力注册表

| 能力 | 处置 |
|---|---|
| `binary_frames` | 保留（0.11 起服务端默认开启、客户端恒声明，可考虑从协商表转为必选语义——**本期不做**，保持协商机制不动） |
| `chunked_http` | **0.11 从 `SERVER_CAPABILITIES` 删除**；`WS_TUNNEL_DISABLE_CAPABILITIES` 对它无效化；1.0 删客户端声明与全部实现 |
| `udp` | 保留 |

### 4.3 wire.json（spec/conformance，现 26 场景）

- 0.11：为 http 家族 10 个场景加 `"deprecated": true` 标注（消费端跳过断言、保留文档价值）：
  `request_full`、`request_minimal_get`、`request_stream_ok`、`response_success`、`response_upstream_error`、`stream_start_sse`、`stream_chunk_with_sequence`、`stream_chunk_encoding_base64`、`stream_end_normal`、`stream_end_error`。
- 1.0：删除上述 10 场景；保留 16 场景（auth 6 + tcp 5 + udp 3 + ping/pong 2）。
- 新增 1 个场景：`legacy_request_message_ignored`（服务端收到 `request` 消息 → 丢弃 + 不断连），锚定 §4.1 的 1.0 行为。

### 4.4 `tunnels.mode` 列

- 0.11：alembic `005_normalize_mode_tcp`——`UPDATE tunnels SET mode='tcp' WHERE mode='http'`；服务端不再读 mode 做路由（`forward()` 已随 deprecation 停用）；`TunnelInfo.mode` 继续返回（admin-console 兼容），恒为 `tcp`。
- 1.0：可选 `006_drop_tunnel_mode`（若 admin-console 已移除展示则一并 drop，否则保留无害列）。

## 5. 服务端清理清单（锚点为当前 HEAD 行号）

### 0.11（deprecation）

- [ ] `SERVER_CAPABILITIES` 删 `chunked_http`（server.py:120）。
- [ ] `/t/`、`/api/tunnels/{domain}/forward` 响应加 `Deprecation`/`Sunset` 头 + 启动时与每次命中打 warning（指向 TCP listener 方案）。
- [ ] `create_tunnel`/`update_tunnel` 忽略 `mode` 入参，恒写 `tcp`；`005` 迁移。
- [ ] 文档：README/QUICKSTART 部署范式改写为「nginx → TCP 端口」；PROTOCOL.md 标注 http 消息族 deprecated。
- [ ] 顺带：AppSettings(TUNELY_) 与 TunnelServerConfig(WS_TUNNEL_) 合并为单一配置源（app.py:67-101 vs config.py:9）——否则「create_full_app 只透传 5 字段」的坑在删 HTTP 面时会暴露得更明显。
- [x] **原生 TLS（控制面）**：`tunely serve` 增加 `--ssl-certfile/--ssl-keyfile`（uvicorn 原生参数透传），`AppSettings` 对应 `TUNELY_SSL_CERT_FILE/TUNELY_SSL_KEY_FILE`——`wss://` 自持。已实现。
- [x] **原生 TLS（数据面）**：**TCP** 监听可选 TLS——`WS_TUNNEL_LISTENER_TLS_CERT_FILE/KEY_FILE` 全局一份（通配符证书，所有监听共用）；配置不完整直接 RuntimeError，不静默降级明文。**UDP 监听不支持 TLS**（stdlib 无 DTLS），配置了证书时显式告警仍为明文。**SSL context 的 ALPN 默认恒锁 `http/1.1`（不可提供 h2）**：TLS 终止后解出的字节原样入隧道，若协商出 h2 而内网目标只讲 HTTP/1.1，即「握手成功但连接废掉」；gRPC（h2）目标可经 `WS_TUNNEL_LISTENER_TLS_ALPN` 覆盖（'h2,http/1.1' / 'none'）。已实现 + ALPN 锁有握手级测试。
- [x] TLS 验收：`python3 tools/e2e_dataplane.py local-tls`（自签证书 + TLS 监听复跑 §10-1 全套字节保真 8/8 PASS，含「客户端提供 h2 仍被锁到 http/1.1」握手断言与控制面 HTTPS 检查）。

### 1.0（removal）

- [ ] app.py：删 `/t/` 双路由（273-293）、catch_all 子域名路由（295-311）、`forward_to_tunnel`、`stream_tunnel_response`、`extract_subdomain`。app 收缩为：admin API（include_router）+ `/health` + WS 端点 + TCP/UDP listener 编排 + lifespan。
- [ ] server.py 路由：删 `POST /api/tunnels/{domain}/forward`（1394-1408，顺带消灭「管理面有鉴权、forward 无鉴权」的 P0 不一致）。
- [ ] server.py 转发机制：删 `forward()`（2214）/`_forward_http`(2297)/`_forward_tcp`(2460)/`forward_stream`(2625)、`_handle_client_tunnel_response`(1821)、`bridge_response_to_stream`(740)、Stream* 处理分支（1724-1747）、`PendingRequest`/`PendingStreamRequest`/`PendingTcpRequest` 及其队列/哨兵/reaper 机制、`_parse_tcp_response`(2566)。
- [ ] server.py 配置消费：删 `default_timeout`、`forward_max_timeout`、`max_pending_requests`、`http_max_response_bytes`、`stream_queue_maxsize`（config.py:34-66 中对应项）；`tcp_max_connections`/`tcp_idle_timeout`/UDP 系全部保留。
- [ ] 请求日志管线：`_enqueue_request_log`/`_write_request_log`/`_request_log_worker`/`_sweep_request_logs` 与 `tunnel_request_logs` 表 **1.0 删除**（TCP 模式无每请求语义；访问日志归 nginx）。0.11 起该表不再增长。
- [ ] protocol.py：删 `TunnelRequest`(109)/`TunnelResponse`(136)/`StreamStart/Chunk/End`(162-218) 模型与 `parse_message_fast` 对应分支；`MessageType` 保留枚举值（旧 wire 兼容识别用），标注 deprecated。
- [ ] 删除 TCP 监听「未绑域名时 fallback 第一个在线隧道」的旧行为（server.py:3122-3125）：TCP-only 后每端口必须显式绑定域名，未绑定一律拒绝连接（消除流量误投任意隧道的 review P2）。
- [ ] 流量统计：唯一缺口「HTTP 模式零计数」随模式消失；TCP/UDP 计数路径保留。

## 6. 客户端清理清单

| | py（0.11 → 1.0） | TS（0.11 → 1.0） | rust（0.11 → 1.0） |
|---|---|---|---|
| `CLIENT_CAPABILITIES` | 去 `chunked_http` | 去 `chunked_http`（protocol.ts:190） | 无需动（本就只有 binary_frames+udp） |
| HTTP 执行路径 | 删 `_execute_request`(695)、SSE/chunked 分支、共享 httpx client | 删 `executeRequest`、SSE/chunked 阈值切换、undici 同源 fetch 特判 | 无（本就没有） |
| TCP/UDP | 已支持，保留 | 已支持 TCP，保留；UDP 不声明照旧 | 已支持，保留 |
| 配置 | `max_response_bytes`/`stream_threshold_bytes` 删（1.0）；`target` 语义改为 `host:port`（兼容继续解析 URL 形式） | 同左（`TUNELY_STREAM_THRESHOLD_BYTES` 等） | 无 |
| wire conformance | 随 wire.json 走 | **借机改为消费 wire.json**（现为手写内联副本），消除三端最后一处人工同步 | 随 wire.json 走 |

- examples/demo.py 重写：`demo_normal_forward`/`demo_sse_forward` 改为「nc/curl → TCP 端口」形态演示。
- py 客户端 `trust_env=False`（httpx 不吃系统代理——review 实测代理环境变量会让每条转发 500）：**0.11 顺带修**。

## 7. 发布形态样例（nginx / 零边缘）

> 生产 nginx 配置目前不入仓（review 已建议入库）。以下样例随本设计首次入库到 `deploy/nginx/`，作为权威参考。占位：portal 监听 `127.0.0.1:8083`、tunely `127.0.0.1:8000`、主站 http 块 `127.0.0.1:8084`（形态 A 下为明文，TLS 已由 stream 终止）。

### 形态 A（推荐）：443 单端口，stream 终止 TLS + SNI 分流

> 关键点：隧道子域名的 TLS **必须在 stream 块终止**——tunely 的 TCP 监听是明文 TCP，
> 若用 `ssl_preread` 直通，`https://dsh.xxx` 的 TLS 握手到达 9080 后无人应答，直接失败。
> 因此 443 整体改为 stream ssl 终止 + `$ssl_server_name` 分流；主站 http 块退为明文内网端口。

```nginx
# ---- nginx.conf 顶层 stream 块：443 TLS 终止 + 按 SNI 分流 ----
stream {
    map $ssl_server_name $sni_backend {
        dsh.agentstudio.cc   127.0.0.1:9080;   # 隧道 dsh（WS_TUNNEL_TCP_LISTEN 绑定）
        foo.agentstudio.cc   127.0.0.1:9081;   # 隧道 foo
        default              127.0.0.1:8084;   # 主站（下方 http 块，含 /ws/tunnel）
    }
    server {
        listen 443 ssl;
        ssl_certificate     /etc/ssl/agentstudio.cc.fullchain.pem;   # 通配符证书
        ssl_certificate_key /etc/ssl/agentstudio.cc.key;
        proxy_pass $sni_backend;
        proxy_timeout 1h;        # 长 SSE/交互流；tunely tcp_idle_timeout 是更细的应用层控制
    }
}

# ---- http 块：主站（stream 已终止 TLS，此处明文，仅绑回环）----
server {
    listen 127.0.0.1:8084;
    server_name dsht.agentstudio.cc;

    # 隧道 WS 控制面：必须公开（认证靠 AuthMessage token，不能套 portal cookie）
    location /ws/tunnel {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;   # map $http_upgrade $connection_upgrade { default upgrade; '' ''; }
        proxy_read_timeout 3600s;
    }

    # 管理 API：仅 console 同源使用（服务端 x-api-key 鉴权）
    location /api/ { proxy_pass http://127.0.0.1:8000; }

    # admin-console 静态资源（portal auth_request 保护）
    location = /_portal_auth {
        internal;
        proxy_pass http://127.0.0.1:8083/auth;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URI $request_uri;
    }
    location /console/ {
        auth_request /_portal_auth;
        error_page 401 =302 /login?next=$request_uri;
        proxy_pass http://127.0.0.1:8001;
    }
    location /login  { proxy_pass http://127.0.0.1:8083; }
    location /logout { proxy_pass http://127.0.0.1:8083; }

    # install.sh 必须可匿名 curl（| bash 链路），/downloads/ 按需保护
    location = /install.sh { proxy_pass http://127.0.0.1:8002; }
    location /downloads/   { auth_request /_portal_auth; proxy_pass http://127.0.0.1:8002; }

    # ── 仅 0.11 deprecation 窗口保留；1.0 整段删除 ──
    location /t/ {
        add_header Deprecation "true" always;
        add_header Sunset "1.0.0" always;
        proxy_pass http://127.0.0.1:8000;
        proxy_buffering off;            # SSE
        proxy_read_timeout 3600s;
    }
}
```

隧道目标即内网服务：`curl https://dsh.agentstudio.cc/api/chat` → nginx SNI → 9080 → 隧道 → 客户端 → `127.0.0.1:8080`，全程字节管道，SSE/上传/二进制天然正确。

### 形态 A-2（可选）：TLS 直通——边缘只见 SNI，隧道流量端到端加密

> 前置：tunely 原生 TLS（§5 0.11）落地。此前直通方案不成立（9080 无人应答 TLS 握手），原生 TLS 后变为合法——TLS 握手由 tunely 应答，nginx 退化为纯 L4 路由器。

```nginx
stream {
    map $ssl_preread_server_name $sni_backend {
        dsh.agentstudio.cc   127.0.0.1:9080;   # tunely 监听自带 TLS（通配符证书）
        foo.agentstudio.cc   127.0.0.1:9081;
        default              127.0.0.1:8084;   # 主站 http 块（listen 8084 ssl，nginx 持主站证书）
    }
    server {
        listen 443;
        ssl_preread on;
        proxy_pass $sni_backend;
        proxy_timeout 1h;
    }
}
```

**适用条件：「边缘不可见隧道内容」是真需求**（nginx 只见 SNI，不解密隧道字节）。不满足就别用，默认推荐形态 A（边缘终止），原因：

- 通配符证书部署两处（nginx 持主站、tunely 持隧道监听），续期需同步；
- ALPN 约束（§5）：h2 目标需 per-listener 覆盖，默认锁 http/1.1；
- 隧道流量不再进 nginx access log，可见性归 tunely/客户端。

### 形态 B：无通配符 DNS，路径前缀（L7 剥前缀转 TCP）

```nginx
# 主站 http 块内（无 stream/SNI 依赖）
location /t/dsh/ {
    proxy_pass http://127.0.0.1:9080/;   # 尾斜杠 = 剥掉 /t/dsh 前缀
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
}
location /t/foo/ { proxy_pass http://127.0.0.1:9081/; }
```

语义与旧 `/t/` 一致（目标服务看到的 path 同样不含前缀），但 body 不再被隧道改写。**这是嵌入式/无通配符场景的标准答案，也是 1.0 删掉服务端 `/t/` 后的替代品。**

### 形态 C（明文直发）：内网/测试/homelab

```yaml
# docker-compose.yml：维持现状即可
ports:
  - "9080:9080"   # 无 nginx、无 TLS；token 保护认证，流量明文——仅限内网或测试
```

### 形态 C'（零依赖公网部署）：tunely 原生 TLS 直发

```yaml
# docker-compose.yml
ports:
  - "443:9443"
environment:
  WS_TUNNEL_TCP_LISTEN: "9443:dsh"
  WS_TUNNEL_TCP_LISTEN_HOST: "0.0.0.0"
  WS_TUNNEL_LISTENER_TLS_CERT_FILE: /certs/fullchain.pem
  WS_TUNNEL_LISTENER_TLS_KEY_FILE: /certs/privkey.pem
  # 控制面同进程：tunely serve --ssl-certfile/--ssl-keyfile 挂同一张证书 → wss://dsh.example.com/ws/tunnel
```

单隧道 / agent 临时隧道的标准形态：**一个进程 + 一张证书，无任何其他组件**。多隧道各占一个公网端口同样成立（共享通配符证书）。只有「单 443 + 多域名 + 浏览器友好 URL」才升级到形态 A/A-2。

## 8. 部署面变更

- [ ] `deploy/nginx/`（新增）：形态 A / A-2 / B / C' 参考配置 + README（含 portal 端口占位说明；A-2 标注适用条件与代价）。
- [ ] `docker-compose.yml`：注释改写（「TCP 出口给 nginx」升格为主路径用法）；`--api-key` 命令行注入改 env 文件（顺带修 review P1，一行改动）。
- [ ] `deploy/README.md` / `QUICKSTART.md`：部署范式改写；删除/标注 `/t/` 与子域名模式章节指向 nginx 样例。
- [ ] `install.sh`：不变（TCP 模式对客户端透明，`SERVER=wss://.../ws/tunnel` 控制面不变）。
- [ ] `ROLLING_UPGRADE.md`：新增本次收敛章节（服务端先行纪律不变；0.11→1.0 的双跑窗口说明）。
- [ ] `CHANGELOG.md`：补 0.8/0.9/0.10 缺失条目（review P2），0.11/1.0 按本设计撰写。

## 9. 版本与发布节奏

| 版本 | py | ts | rust | 内容 |
|---|---|---|---|---|
| **0.11.0** | 0.11.0 | 0.5.0 | 0.4.x 不发或 0.5.0 | deprecation：能力摘除、Deprecation 头、`005` 迁移、**原生 TLS（控制面 + 监听）**、文档改口、nginx 样例入库；顺带：py 主动 ping、`trust_env=False`、配置合并 |
| **1.0.0** | 1.0.0 | 1.0.0 | 1.0.0 | removal：§5/§6 全部删除项落地、wire.json 删 10 场景、`006` 可选迁移；三端同步 tag |

发布顺序（每步全绿再下一步，沿用 PROTOCOL_V2 分期纪律）：

1. **0.11 服务端**先行发布（旧客户端全兼容）→ 部署到 dsht。
2. nginx 形态 A/B 配置上线，**现网隧道切到 TCP 出口**（此时 `/t/` 仍在，可随时回切）。
3. 三客户端 0.11 发版（py/ts 摘除 chunked_http 声明）。
4. 观察一个周期（建议 ≥2 周）：`/t/` 命中日志应归零。
5. **1.0** 三端同步：删码、wire 清理、tag `python-v1.0.0`。

## 10. 测试与验收

**删除**（1.0）：`test_chunked_http.py` 整文件；`test_tunnel_mode.py` http 部分；`test_server.py`/`test_stability_p1.py`/`test_server_observability.py` 中 forward/stream/request-log 用例；`test_wire_conformance.py` 自动随 wire.json 收缩。原则：**每删一个用例必须在 §5/§6 清单中有对应删除项**，防止「删测试但留代码」。

**新增**（0.11 就位，作为收敛的核心验收——正好补上 review 指出的「/t/ 零测试」的镜像缺口）：

1. **e2e 字节保真**：`curl → nginx(形态B) → TCP listener → client → target`，断言：
   - JSON 响应逐字节一致（旧 HTTP 模式 0 字节事故的反向锚定）；
   - gzip/二进制（256B 随机）逐字节一致、`content-encoding` 原样到达；
   - SSE 事件边界与 `event:` 类型保真（nginx `proxy_buffering off`）；
   - 100MB chunked 上传流式到达（服务端内存平稳）；
   - 嵌套 WebSocket 冒烟（经隧道建 wss）。
2. **兼容矩阵冒烟**（4 格）：新/旧 server × 新/旧 client 各跑通 TCP 隧道 + auth。
3. **legacy 消息丢弃**：1.0 后服务端收到 `request/response/stream_*` → warning + 不断连（wire 场景 `legacy_request_message_ignored`）。
4. admin-console：`mode` 展示为 `tcp` 不破版（现无 `/forward` 调用点，已核实）。

## 11. 顺带修复项（借收敛之机，范围严格限定）

- py 客户端补**主动 ping**（当前只应答不发起，导致服务端 120s 判定退化，review P1）；服务端消费 `heartbeat_interval/heartbeat_timeout` 配置做 ping sweeper 或删除该配置——二选一，推荐前者。
- py httpx `trust_env=False`。
- AppSettings/TunnelServerConfig 合并（见 §5 0.11）。
- compose `--api-key` 改 env 注入。

**明确范围外**（另立 issue，避免本次失控）：token 哈希化落库、install.sh 加固、portal 密码哈希/会话吊销、admin-console localStorage 凭据、`redis_url` 假配置删除（1.0 顺手删）。

## 12. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 现网 dsht 实际还在依赖 `/t/` 缓冲路径 | 迁移第 2 步前先跑 §10-1 的 e2e 脚本打现网 `/t/`，量化踩坑面；nginx 新旧 location 并行，按路径灰度切换 |
| 无 nginx 的嵌入式 SDK 用户失去 `/t/` | 0.11 文档明示 + 提供形态 B 样例；deprecation 窗口 ≥1 minor；`forward()` 在仓内无消费方（已核实） |
| SSE/长连接被 nginx `proxy_timeout` 误杀 | 样例给 1h；文档写明与 `tcp_idle_timeout` 的关系（nginx ≥ tunely） |
| 删码后「管理面可见性」下降（无每请求日志） | 访问日志归 nginx（本来就有）；`tunnel_request_logs` 的 30 天留存需求移交 nginx 日志轮转 |
| 回滚 | 0.11 窗口内 = 配置回切（`/t/` 路由还在）。注意 `005` 归一 mode 为单向：0.10 的 `forward()` 按注册时缓存的 mode 路由，归一后回退旧版会破坏 `/t/`——**执行 005 前先备份 tunnels 表的 mode 列**；1.0 后回滚 = 回退镜像版本 + 手工恢复 mode |
