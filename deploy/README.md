# deploy/ — Tunely 部署模板

> 本目录全部为**模板，需按环境替换**：所有 `<尖括号>` 占位符必须改成实际值后再使用。

| 文件 | 用途 |
|------|------|
| `tunely-server.service` | Linux systemd · 服务端（Python `tunely serve`） |
| `tunely-client.service` | Linux systemd · 客户端（Rust `tunely connect`） |
| `com.tunely.client.plist` | macOS launchd · 客户端（Rust `tunely connect`） |

## 1. 服务端（systemd）

```bash
# 安装服务端 CLI（二进制路径需与 unit 中 ExecStart 一致）
uv tool install tunely    # 或 pipx install tunely

# 准备账号与目录
sudo useradd -r -s /usr/sbin/nologin tunely
sudo mkdir -p /var/lib/tunely/data /etc/tunely
sudo chown -R tunely:tunely /var/lib/tunely

# 写环境变量（见下文 /etc/tunely/env），然后安装 unit
sudo cp deploy/tunely-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tunely-server
journalctl -u tunely-server -f
```

要点：

- **`/etc/tunely/env`** 存放所有 `WS_TUNNEL_*` 环境变量（服务端配置的 env 前缀是 `WS_TUNNEL_`）。
- **`ExecStart` 用命令行参数**传 `--host/--port/--domain/--database`——`serve` CLI 不读 `TUNELY_*` 环境变量。
- 管理 API 密钥优先依赖 env 的 `WS_TUNNEL_ADMIN_API_KEY`（0.6.0 起 serve 会读它）；若你的版本不生效（0.5.x 中 serve 显式传 None 会覆盖 env），按 unit 内注释改用 `--api-key` 命令行传入。

### `/etc/tunely/env` 示例

```bash
# Tunely Server 环境变量（每行 KEY=VALUE，# 开头为注释；建议 chmod 600、属主 tunely）

# TCP 多端口监听："端口:隧道域名"，逗号分隔；每端口固定绑一条隧道
WS_TUNNEL_TCP_LISTEN="9080:dsh,9081:foo"
# TCP 监听绑定地址（默认 0.0.0.0）
WS_TUNNEL_TCP_LISTEN_HOST="0.0.0.0"
# TCP 每连接并发上限（仅部分版本支持；不认识的 WS_TUNNEL_* 变量会被服务端忽略，不影响启动）
WS_TUNNEL_TCP_MAX_CONNECTIONS="64"

# 管理 API 密钥（创建/查询/删除隧道时需携带 x-api-key）
WS_TUNNEL_ADMIN_API_KEY="<换成长随机串>"
# 公网模式下创建隧道需 JWT 认证（可选）
#WS_TUNNEL_JWT_SECRET="<换成长随机串>"
# 心跳（秒，可选）
#WS_TUNNEL_HEARTBEAT_INTERVAL="30"
#WS_TUNNEL_HEARTBEAT_TIMEOUT="90"
```

> 旧式单端口写法（新写法优先）：`WS_TUNNEL_TCP_LISTEN_PORT="9080"` + `WS_TUNNEL_TCP_TARGET_DOMAIN="dsh"`。

## 2. 客户端（systemd / launchd）

**客户端需要先安装 Rust 版 CLI：**

```bash
cargo install tunely
# systemd 部署时把二进制放到 unit 引用的路径：
sudo ln -sf ~/.cargo/bin/tunely /usr/local/bin/tunely
```

Linux 用 `deploy/tunely-client.service`，macOS 用 `deploy/com.tunely.client.plist`，两者都读取同一个客户端配置文件 `/etc/tunely/client.toml`（macOS 也可放到其他路径，改 ProgramArguments 即可）。

### `/etc/tunely/client.toml` 示例

```toml
# Tunely Rust 客户端配置（字段与 connect 旗标一一对应）
server = "ws://tunely.example.com/ws/tunnel"   # 服务端 WebSocket URL
token = "tun_xxxxxxxxxxxx"                     # 隧道令牌（服务端 tunnel create 输出），必填
target = "http://127.0.0.1:8080"               # 本地目标服务 URL
reconnect = 5                                  # 重连间隔（秒）
max_reconnect = 0                              # 最大重连次数，0 = 无限
request_timeout = 300                          # 单请求超时（秒）
force = false                                  # true = 强制抢占已有连接
```

> **注意**：当前源码中的 Rust CLI 只支持旗标形式（`tunely connect --token ... --server ... --target ...`），尚无 `--config` 参数。模板 ExecStart 按任务要求写了 `--config /etc/tunely/client.toml`；在支持 `--config` 的版本发布前，请改用各模板内注释里的旗标形式 ExecStart/ProgramArguments。

## 3. 隧道的创建与连接

```bash
# 服务端上创建隧道（得到 token）
tunely tunnel create <dsh> --server http://<127.0.0.1>:<8000> --api-key <your-admin-api-key>

# 客户端连接
tunely connect --token <tun_xxx> --server ws://<tunely.example.com>/ws/tunnel --target http://127.0.0.1:8080
```

之后访问 `http://dsh.<tunely.example.com>/`（子域名模式）或 `http://<tunely.example.com>/t/<dsh>/`（路径前缀模式）；若配置了 `WS_TUNNEL_TCP_LISTEN`，也可直连对应 TCP 端口。

## 4. 备份与恢复

隧道/令牌等状态都在 SQLite（模板路径 `/var/lib/tunely/data/tunely.db`）。**令牌即凭据，备份文件请按敏感数据处理（权限 600）。**

**方式一：在线备份（推荐，不停服）**

SQLite 自带的 `.backup` 命令在线生成一致性快照：

```bash
sudo -u tunely sqlite3 /var/lib/tunely/data/tunely.db \
  ".backup '/var/backups/tunely-$(date +%F).db'"
```

建议加 cron 每日一次，并保留最近 N 份。

**方式二：停服冷备**

```bash
sudo systemctl stop tunely-server
sudo cp /var/lib/tunely/data/tunely.db /var/backups/tunely-cold.db
sudo systemctl start tunely-server
```

**恢复**

```bash
sudo systemctl stop tunely-server
sudo -u tunely sqlite3 /var/lib/tunely/data/tunely.db "PRAGMA integrity_check;"   # 恢复前先确认坏没坏
sudo cp /var/backups/tunely-<date>.db /var/lib/tunely/data/tunely.db
sudo chown tunely:tunely /var/lib/tunely/data/tunely.db
sudo systemctl start tunely-server
journalctl -u tunely-server -n 50 --no-pager    # 确认启动无迁移/损坏报错
curl -s http://127.0.0.1:<8000>/api/tunnels -H "x-api-key: <key>"   # 确认隧道列表完整
```

> 若使用 MySQL/PostgreSQL，改用各自原生备份工具（`mysqldump` / `pg_dump`），思路相同。

## 5. 日志轮转

服务端与客户端均由 systemd 拉起，日志进 **journald**，天然按 journal 自身机制管理，**无需 logrotate**。需要控制的是磁盘占用：

```bash
# 一次性清理：只保留 200M
sudo journalctl --vacuum-size=200M

# 持久限制：/etc/systemd/journald.conf 设
#   SystemMaxUse=200M
# 然后重启 journald 生效
sudo systemctl restart systemd-journald
```

查看与过滤：

```bash
journalctl -u tunely-server -f          # 跟随服务端日志
journalctl -u tunely-client --since today
journalctl -u tunely-server -p warning  # 只看告警以上
```

> journald 默认重启后可能丢历史日志；如需持久化，确保 `/var/log/journal/` 存在（`Storage=persistent`）。

## 6. 探测面加固（Anti-Probe Hardening，运维侧 T8）

> SDK 侧去特征化（0.12）已落地，见 [`docs/PROBE_HARDENING.md`](../docs/PROBE_HARDENING.md)。
> 本节是**部署侧**配套动作，目标是：主动扫描公网入口只看到一个普通站点，而不是一台隧道服务器。

### 6.1 清单（按优先级）

1. **必须配置 `WS_TUNNEL_ADMIN_API_KEY`**（长随机串）：0.12 起 `/api/info`、`/metrics` 受其门控；**不配置则这两端点仍开放**（与既有 admin 端点语义一致）。
2. **wss-only**：控制面务必走 TLS（`serve --ssl-certfile/--ssl-keyfile` 原生 TLS，或边缘 nginx TLS）。裸 `ws://` 下协议 JSON、base64 数据明文可读，秒识别。
3. **`ws_path` 随机化**：默认 `/ws/tunnel` 是可猜测路径。`serve --ws-path /<随机段>/ws/tunnel`，客户端 `server` URL 同步修改。
4. **入口收敛 + decoy**：根路径配置像样的落地页（见 6.4 的 `TUNELY_ROOT_RESPONSE_FILE`；原生无 nginx 时也生效），不要用「空白 404」——那本身就是特征。
5. **域名拆分**：portal/downloads 所在域名与隧道 wss 入口域名分离（见 6.5）。
6. **TLS 细节取舍**：见 6.3（ALPN 默认恒锁 `http/1.1` 属弱指纹，记录在案）。

### 6.2 nginx 参考配置（边缘反代形态）

只反代必要路径，其余全部落到 decoy 静态站：

```nginx
server {
    listen 443 ssl;
    server_name <隧道域名>;
    server_tokens off;                 # Server 头只露 "nginx"，不带版本

    # 不向客户端透传上游 Server 头（uvicorn 指纹）
    proxy_hide_header Server;

    # —— 控制面 WebSocket：精确匹配随机化路径 ——
    location = /<随机段>/ws/tunnel {    # 与 serve --ws-path 一致
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;      # 长连接：需大于心跳超时（默认 90s）
    }

    # —— 路径前缀转发与管理 API ——
    location /t/   { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
    location /api/ { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
    location = /health { proxy_pass http://127.0.0.1:8000; }

    # —— decoy：像样的静态落地页，其余路径全部落到这里 ——
    location / { root /var/www/decoy-site; index index.html; }
}
```

### 6.3 原生 TLS（0.11，无边缘反代）的取舍

- `serve --ssl-certfile/--ssl-keyfile` 单进程终止 TLS，零边缘部署；
- 监听 TLS 的 ALPN 由 `WS_TUNNEL_LISTENER_TLS_ALPN` 控制，**默认恒锁 `http/1.1`**——真实站点普遍 h2+http/1.1 并存，属弱指纹；仅 gRPC(h2) 内网目标才需要改；
- 原生 TLS 模式没有 nginx 兜底，`Server: uvicorn` 头会直出；在意就在前面加一层 6.2 的 nginx；
- 原生模式下无「其余路径落 decoy」能力，根路径 decoy 靠 `TUNELY_ROOT_RESPONSE_FILE`（下文）。

### 6.4 根路径 decoy（`TUNELY_ROOT_RESPONSE_FILE`）

```bash
# /etc/tunely/env 追加（AppSettings 读取，serve/uvicorn 直启均生效）
TUNELY_ROOT_RESPONSE_FILE="/var/www/decoy-site/index.html"
```

配置后主域名 `GET /` 返回该 HTML（`text/html`）；不配置默认回极简 `OK`。子域名转发行为不受影响。

### 6.5 域名拆分

- portal/downloads 所在域名与隧道 wss 域名**分离**：portal 已有登录鉴权，但不要与隧道入口混布在同一 server 块 / 同一证书语义下；
- dsht 实例的 portal 源码、install.sh 与下载产物已迁至独立运维仓 `~/projects/crypto-ops`（本仓只剩通用模板，见该仓 README 的发布纪律）；
- 效果：扫描 portal 域名看不到隧道端点，扫描隧道域名拿不到客户端二进制。

### 6.6 验收（复测命令）

```bash
# 以下全部应当「像普通站点」：
curl -s https://<隧道域名>/                  # decoy 落地页或 OK；无 tunely/version/domain 字样
curl -s https://<隧道域名>/health            # {"status":"ok"}，无 connected_tunnels
curl -s -o /dev/null -w "%{http_code}\n" https://<隧道域名>/api/info    # 401（配置 admin key 后）
curl -s -o /dev/null -w "%{http_code}\n" https://<隧道域名>/metrics     # 401
curl -s -o /dev/null -w "%{http_code}\n" https://<隧道域名>/docs        # 404
curl -s -o /dev/null -w "%{http_code}\n" https://<隧道域名>/openapi.json # 404
curl -sI https://<隧道域名>/ | grep -i '^server'  # 只见 nginx，无 uvicorn/tunely

# WS：无 token / 错 token / 禁用 token 的连接都应回统一文案
# {"type":"auth_error","error":"Authentication failed","code":"auth_failed"}
# 并在 0.8–1.6s 随机延迟后以 1008 关闭——相互时序不可区分（token 枚举 oracle 已消除）
```
