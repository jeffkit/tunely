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
