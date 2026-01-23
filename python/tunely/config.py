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
    default_timeout: float = Field(default=300.0, description="默认请求超时（秒）")
    max_pending_requests: int = Field(default=1000, description="最大待处理请求数")

    # 分布式配置（可选）
    redis_url: str | None = Field(
        default=None, description="Redis URL（用于分布式部署）"
    )
    node_id: str | None = Field(default=None, description="节点标识（分布式部署时）")

    # 安全配置
    admin_api_key: str | None = Field(
        default=None, description="管理 API 密钥（用于创建/删除隧道）"
    )

    # 用户提示信息
    instruction: str | None = Field(
        default=None, description="接入用户须知说明（在 /api/info 接口中返回）"
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
    request_timeout: float = Field(default=300.0, description="请求超时（秒）")

    model_config = {
        "env_prefix": "WS_TUNNEL_CLIENT_",
        "env_file": ".env",
        "extra": "ignore",
    }
