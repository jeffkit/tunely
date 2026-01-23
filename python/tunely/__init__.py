"""
WS-Tunnel - WebSocket 透明反向代理隧道

提供服务端 SDK 和客户端 SDK，支持：
- 服务端嵌入到 FastAPI 应用
- 客户端独立运行或嵌入应用
- 预注册隧道 + Token 认证
- SSE (Server-Sent Events) 流式响应
- 分布式部署（可选 Redis）
"""

__version__ = "0.2.0"

from .protocol import (
    TunnelRequest,
    TunnelResponse,
    AuthMessage,
    AuthOkMessage,
    AuthErrorMessage,
    PingMessage,
    PongMessage,
    MessageType,
    # 流式响应
    StreamStartMessage,
    StreamChunkMessage,
    StreamEndMessage,
)
from .server import TunnelServer, TunnelManager
from .client import TunnelClient
from .config import TunnelServerConfig, TunnelClientConfig
from .app import create_full_app, run_app

__all__ = [
    # 版本
    "__version__",
    # 协议
    "TunnelRequest",
    "TunnelResponse",
    "AuthMessage",
    "AuthOkMessage",
    "AuthErrorMessage",
    "PingMessage",
    "PongMessage",
    "MessageType",
    # 流式响应
    "StreamStartMessage",
    "StreamChunkMessage",
    "StreamEndMessage",
    # 服务端
    "TunnelServer",
    "TunnelManager",
    "TunnelServerConfig",
    # 客户端
    "TunnelClient",
    "TunnelClientConfig",
    # 应用
    "create_full_app",
    "run_app",
]
