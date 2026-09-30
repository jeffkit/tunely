"""
WS-Tunnel 配置
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class TunnelServerConfig(BaseSettings):
    """服务端配置"""

    # 域名配置
    domain: str = Field(
        default="localhost",
        description="顶级域名（用于子域名路由，如 tunely.woa.com）",
    )

    # 数据库
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/tunnels.db",
        description="数据库连接 URL（支持 SQLite, MySQL, PostgreSQL）",
    )

    # WebSocket 配置
    ws_path: str = Field(default="/ws/tunnel", description="WebSocket 端点路径")
    ws_url: str | None = Field(
        default=None,
        description="WebSocket 完整 URL（可选，用于覆盖自动生成的 URL）",
    )
    heartbeat_interval: int = Field(default=30, description="心跳间隔（秒）")
    heartbeat_timeout: int = Field(default=90, description="心跳超时（秒）")

    # 请求配置
    default_timeout: float = Field(default=1800.0, description="默认请求超时（秒）")
    max_pending_requests: int = Field(
        default=1000,
        description="最大待处理请求数（forward 转发面限额，达到后返回 503）",
    )
    forward_max_timeout: float = Field(
        default=600.0,
        description="forward 转发超时上限（秒）：对请求传入的 timeout 做 clamp，防止长期占用；0 = 不限制（env: WS_TUNNEL_FORWARD_MAX_TIMEOUT）",
    )

    # 请求日志（落库走后台队列，不阻塞转发面）
    request_log_enabled: bool = Field(
        default=True,
        description="是否把每请求日志写库（env: WS_TUNNEL_REQUEST_LOG_ENABLED）；关闭后转发面对请求日志零 DB 写",
    )
    request_log_retention_days: int = Field(
        default=30,
        description="请求日志保留天数，后台任务周期清理超期行；0 = 永久保留（env: WS_TUNNEL_REQUEST_LOG_RETENTION_DAYS）",
    )

    # 内存安全上限（防 OOM）
    tcp_forward_max_buffer_bytes: int = Field(
        default=10485760,
        description="HTTP 触发的 TCP 转发单请求响应累积缓冲上限（字节），超限该请求以 'response too large' 失败；0 = 不限制（env: WS_TUNNEL_TCP_FORWARD_MAX_BUFFER_BYTES）",
    )
    http_max_response_bytes: int = Field(
        default=104857600,
        description="HTTP 模式单请求响应体上限（字节），超限以 502 拒绝（客户端/服务端都不再缓冲超限体）；0 = 不限制（env: WS_TUNNEL_HTTP_MAX_RESPONSE_BYTES）",
    )
    stream_queue_maxsize: int = Field(
        default=1024,
        description="流式响应（SSE）单请求数据块队列上限，生产侧写满即按流错误结束该流；0 = 不限制（env: WS_TUNNEL_STREAM_QUEUE_MAXSIZE）",
    )

    # 分布式配置（可选）
    redis_url: str | None = Field(
        default=None, description="Redis URL（用于分布式部署）"
    )
    node_id: str | None = Field(default=None, description="节点标识（分布式部署时）")

    # 安全配置
    admin_api_key: str | None = Field(
        default=None, description="管理 API 密钥（用于创建/删除隧道）"
    )

    # TCP 监听配置（可选，启用后会监听 TCP 端口并通过隧道转发）
    tcp_listen_port: int | None = Field(
        default=None, description="TCP 监听端口（如果设置，服务端会监听此端口并转发到隧道客户端）"
    )
    tcp_listen_host: str = Field(
        default="127.0.0.1",
        description="TCP 监听地址（默认仅回环，安全默认值；容器/公网部署需显式设为 0.0.0.0）",
    )
    tcp_target_domain: str | None = Field(
        default=None, description="TCP 转发目标域名（必须与某个隧道域名匹配）"
    )
    tcp_listen: str | None = Field(
        default=None,
        description="多监听器配置（可选）：'port:domain[,port:domain...]'，"
        "每个端口固定绑定一条隧道；与 tcp_listen_port 可并用，同端口时本字段优先",
    )
    tcp_max_connections: int = Field(
        default=0,
        description="每隧道 TCP 并发连接上限（0 表示不限制；env: WS_TUNNEL_TCP_MAX_CONNECTIONS）",
    )
    tcp_idle_timeout: int = Field(
        default=300,
        description="外部 TCP 连接空闲超时（秒）：连上不发数据的慢连接超时后被服务端关闭回收；0 = 不启用（env: WS_TUNNEL_TCP_IDLE_TIMEOUT）",
    )

    # 监听原生 TLS（TCP-only 收敛 0.11，docs/MIGRATION_TCP_ONLY.md §5；
    # 作用于全部 TCP 监听，通常配通配符证书一份；控制面 TLS 走 serve --ssl-certfile）
    listener_tls_cert_file: str | None = Field(
        default=None,
        description="TCP 监听 TLS 证书（PEM，全监听共用一份；与 key 成对配置才启用）（env: WS_TUNNEL_LISTENER_TLS_CERT_FILE）",
    )
    listener_tls_key_file: str | None = Field(
        default=None,
        description="TCP 监听 TLS 私钥（PEM）（env: WS_TUNNEL_LISTENER_TLS_KEY_FILE）",
    )
    listener_tls_alpn: str = Field(
        default="http/1.1",
        description="监听 TLS 的 ALPN 协议列表（逗号分隔）。默认恒锁 http/1.1——TLS 终止后"
        "解出的字节原样入隧道，协商出 h2 而内网目标只讲 HTTP/1.1 即断连；"
        "gRPC(h2) 目标可改 'h2'；'none' = 不做 ALPN（env: WS_TUNNEL_LISTENER_TLS_ALPN）",
    )

    # UDP 监听配置（协议 v2 udp 能力，可选；启用后经隧道转发 UDP 数据报）
    udp_listen: str | None = Field(
        default=None,
        description="UDP 多监听器配置（可选）：'port:domain[,port:domain...]'，"
        "每端口固定绑定一条隧道；与 TCP 监听同格式、独立 env，UDP/TCP 可同端口号并存；"
        "监听地址复用 tcp_listen_host（默认仅回环）（env: WS_TUNNEL_UDP_LISTEN）",
    )
    udp_session_timeout: int = Field(
        default=60,
        description="UDP 会话空闲超时（秒）：超时无包的会话由周期 sweeper 回收"
        "（发 udp_close + 删映射）；0 = 不限（env: WS_TUNNEL_UDP_SESSION_TIMEOUT）",
    )
    udp_max_sessions: int = Field(
        default=256,
        description="每隧道 UDP 会话数上限（0 = 不限），超限丢包——UDP 无连接，"
        "防会话表被扫爆（反射放大风险见 PROTOCOL.md「UDP 会话语义」）"
        "（env: WS_TUNNEL_UDP_MAX_SESSIONS）",
    )

    # 协议 v2 能力协商（kill 开关）
    disable_capabilities: str | None = Field(
        default=None,
        description="逗号分隔的能力名，从服务端能力注册表剔除（kill 开关）；env: WS_TUNNEL_DISABLE_CAPABILITIES",
    )

    # JWT 认证（公网模式：需要 JWT 令牌才能创建隧道）
    jwt_secret: str | None = Field(
        default=None, description="JWT 共享密钥（设置后创建隧道需要 Bearer JWT 认证）"
    )

    # 用户提示信息
    instruction: str | None = Field(
        default=None, description="接入用户须知说明（在 /api/info 接口中返回）"
    )

    # Webhook 通知（用于 as-dispatch ACN Inbox 集成）
    dispatch_webhook_url: str | None = Field(
        default=None,
        description="as-dispatch webhook 地址（如 http://localhost:8083），设置后客户端连接时会发送通知",
    )
    dispatch_webhook_secret: str | None = Field(
        default=None,
        description="Webhook HMAC-SHA256 共享密钥（env: WS_TUNNEL_DISPATCH_WEBHOOK_SECRET），设置后每次请求附加 X-Webhook-Signature 头",
    )

    model_config = {
        "env_prefix": "WS_TUNNEL_",
        "env_file": ".env",
        "extra": "ignore",
    }


class TunnelClientConfig(BaseSettings):
    """客户端配置"""

    # 服务端连接
    server_url: str = Field(
        default="ws://localhost:8000/ws/tunnel", description="服务端 WebSocket URL"
    )
    token: str = Field(..., description="隧道令牌")

    # 目标服务
    target_url: str = Field(
        default="http://localhost:8080", description="本地目标服务 URL"
    )

    # 连接配置
    reconnect_interval: float = Field(default=5.0, description="重连间隔（秒）")
    max_reconnect_attempts: int = Field(default=0, description="最大重连次数（0 表示无限）")
    force: bool = Field(default=False, description="是否强制抢占已有连接")

    # 请求配置
    request_timeout: float = Field(default=1800.0, description="请求超时（秒）")
    max_response_bytes: int = Field(
        default=104857600,
        description="普通响应体内存上限（字节），超限中止读取并以 502 返回（与生产服务端 cap 对齐）；0 = 不限制（env: WS_TUNNEL_CLIENT_MAX_RESPONSE_BYTES）",
    )
    stream_threshold_bytes: int = Field(
        default=1048576,
        description="非 SSE 响应超过该字节数、且协商了 chunked_http、且服务端放行"
        "（request.stream_ok）时切换流式回传（协议 v2 chunked_http）；"
        "0 = 从不流式（env: WS_TUNNEL_CLIENT_STREAM_THRESHOLD_BYTES）",
    )

    # 多隧道模式下的会话标签（单隧道为 None）；用于日志前缀
    name: str | None = Field(default=None, description="多隧道会话标签")

    model_config = {
        "env_prefix": "WS_TUNNEL_CLIENT_",
        "env_file": ".env",
        "extra": "ignore",
    }


# ============== 多隧道形态（TOML [[tunnel]]，与 rust 客户端同构） ==============

_DEFAULT_SERVER = "ws://localhost:8000/ws/tunnel"
_DEFAULT_TARGET = "http://localhost:8080"


def load_client_settings_from_toml(
    path: str,
    cli_server: str | None = None,
    cli_token: str | None = None,
    cli_target: str | None = None,
    cli_reconnect: float | None = None,
    cli_force: bool | None = None,
) -> list[dict]:
    """读取 connect 的 TOML 配置文件，解析出 TunnelClientConfig 字段字典列表。

    形态（与 rust 客户端 client.toml 同键名）::

        server = "wss://..."
        target = "http://..."            # 可作 [[tunnel]] 各条目的回退
        reconnect_secs = 5
        force = false

        [[tunnel]]
        name = "dsh"                     # 可选，缺省 tunnel-N
        token = "tun_..."
        target = "http://..."            # 可选

    无 [[tunnel]] 时为单隧道形态（顶层 token/target）。多隧道形态下
    禁止单隧道 CLI 来源（--token/--target），避免两种形态静默混用。

    返回值是 TunnelClientConfig(**item) 可直接展开的字段字典列表
    （含 name，单隧道为 None）。
    """
    import tomllib

    with open(path, "rb") as f:
        data = tomllib.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"配置文件 {path} 内容不是 TOML 表")

    entries = data.get("tunnel") or []
    if entries and (cli_token or cli_target):
        raise ValueError("配置了 [[tunnel]] 多隧道数组时不能再指定单隧道参数 --token/--target")

    server = cli_server or data.get("server") or _DEFAULT_SERVER
    reconnect = (
        cli_reconnect
        if cli_reconnect is not None
        else float(data.get("reconnect_secs", 5.0))
    )
    force = bool(cli_force) or bool(data.get("force", False))
    max_reconnect = int(data.get("max_reconnect", 0))

    def _base(name: str | None, token: str, target: str) -> dict:
        return {
            "server_url": server,
            "token": token,
            "target_url": target,
            "reconnect_interval": reconnect,
            "max_reconnect_attempts": max_reconnect,
            "force": force,
            "name": name,
        }

    if not entries:
        token = (cli_token or data.get("token") or "").strip()
        if not token:
            raise ValueError("缺少 token：请通过 --token 或配置文件提供")
        target = cli_target or data.get("target") or _DEFAULT_TARGET
        return [_base(None, token, target)]

    out: list[dict] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"[[tunnel]] 第 {i + 1} 条不是表")
        token = (entry.get("token") or "").strip()
        if not token:
            raise ValueError(f"[[tunnel]] 第 {i + 1} 条缺少 token")
        name = entry.get("name") or f"tunnel-{i + 1}"
        target = entry.get("target") or data.get("target") or _DEFAULT_TARGET
        out.append(_base(name, token, target))
    return out
