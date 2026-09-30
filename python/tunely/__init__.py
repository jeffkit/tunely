"""
WS-Tunnel - WebSocket 透明反向代理隧道

提供服务端 SDK 和客户端 SDK，支持：
- 服务端嵌入到 FastAPI 应用
- 客户端独立运行或嵌入应用
- 预注册隧道 + Token 认证
- SSE (Server-Sent Events) 流式响应
- 分布式部署（可选 Redis）

导入策略（PEP 562）：
    协议层（protocol.py）、客户端（client.py）、配置（config.py）为轻量模块，
    直接 eager import；服务端（server.py，依赖 fastapi/sqlalchemy）与独立应用
    （app.py，依赖 fastapi/pydantic-settings）只在首次访问对应属性时惰性导入。
    因此 `from tunely.client import TunnelClient` 或 `import tunely` 不会把
    服务端依赖拉进进程。
"""

import importlib

__version__ = "0.11.0"

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
from .client import TunnelClient
from .config import TunnelServerConfig, TunnelClientConfig

# 惰性导入表：属性名 -> (子模块名, 子模块内属性名)
# server / app 依赖 fastapi、sqlalchemy 等重依赖，仅在首次访问时才导入。
_LAZY_IMPORTS = {
    # 服务端
    "TunnelServer": (".server", "TunnelServer"),
    "TunnelManager": (".server", "TunnelManager"),
    # 应用
    "create_full_app": (".app", "create_full_app"),
    "run_app": (".app", "run_app"),
}

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


def __getattr__(name: str):
    """PEP 562 模块级 __getattr__：按需导入服务端 / 应用符号。

    首次访问后把结果缓存进模块 globals()，后续访问走正常的模块属性查找，
    不再经过 __getattr__。
    """
    try:
        module_name, attr_name = _LAZY_IMPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    """让尚未触发的惰性属性也能出现在 dir(tunely) 中。"""
    return sorted(set(globals()) | set(_LAZY_IMPORTS))
