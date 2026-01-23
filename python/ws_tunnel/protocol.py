"""
WS-Tunnel 协议定义

协议版本: 1.0

消息类型:
- auth: 客户端认证请求
- auth_ok: 服务端认证成功响应
- auth_error: 服务端认证失败响应
- request: 服务端发送的 HTTP 请求
- response: 客户端返回的 HTTP 响应
- ping/pong: 心跳保活
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """消息类型"""

    # 认证
    AUTH = "auth"
    AUTH_OK = "auth_ok"
    AUTH_ERROR = "auth_error"

    # 请求-响应
    REQUEST = "request"
    RESPONSE = "response"

    # 心跳
    PING = "ping"
    PONG = "pong"


# ============== 认证消息 ==============


class AuthMessage(BaseModel):
    """客户端认证请求"""

    type: MessageType = MessageType.AUTH
    token: str = Field(..., description="隧道令牌")
    client_version: str = Field(default="0.1.0", description="客户端版本")


class AuthOkMessage(BaseModel):
    """认证成功响应"""

    type: MessageType = MessageType.AUTH_OK
    domain: str = Field(..., description="分配的域名")
    tunnel_id: str = Field(..., description="隧道 ID")
    server_version: str = Field(default="0.1.0", description="服务端版本")


class AuthErrorMessage(BaseModel):
    """认证失败响应"""

    type: MessageType = MessageType.AUTH_ERROR
    error: str = Field(..., description="错误信息")
    code: str = Field(default="auth_failed", description="错误代码")


# ============== 请求-响应消息 ==============


class TunnelRequest(BaseModel):
    """
    HTTP 请求（服务端 → 客户端）

    服务端将 HTTP 请求序列化后通过 WebSocket 发送给客户端
    """

    type: MessageType = MessageType.REQUEST
    id: str = Field(..., description="请求唯一 ID，用于匹配响应")
    method: str = Field(..., description="HTTP 方法: GET, POST, PUT, DELETE 等")
    path: str = Field(..., description="请求路径，如 /api/chat")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP 请求头")
    body: str | None = Field(default=None, description="请求体（JSON 字符串或其他）")
    timeout: float = Field(default=300.0, description="超时时间（秒）")

    # 元信息
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="请求时间"
    )


class TunnelResponse(BaseModel):
    """
    HTTP 响应（客户端 → 服务端）

    客户端执行 HTTP 请求后，将响应序列化返回给服务端
    """

    type: MessageType = MessageType.RESPONSE
    id: str = Field(..., description="请求 ID，与 TunnelRequest.id 对应")
    status: int = Field(..., description="HTTP 状态码")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP 响应头")
    body: str | None = Field(default=None, description="响应体")

    # 错误信息（如果请求失败）
    error: str | None = Field(default=None, description="错误信息（如果请求失败）")

    # 元信息
    duration_ms: int = Field(default=0, description="请求耗时（毫秒）")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="响应时间"
    )


# ============== 心跳消息 ==============


class PingMessage(BaseModel):
    """心跳请求"""

    type: MessageType = MessageType.PING
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="发送时间"
    )


class PongMessage(BaseModel):
    """心跳响应"""

    type: MessageType = MessageType.PONG
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="响应时间"
    )


# ============== 消息解析 ==============


def parse_message(data: dict[str, Any]) -> BaseModel:
    """
    解析消息

    Args:
        data: JSON 解析后的字典

    Returns:
        对应类型的消息对象

    Raises:
        ValueError: 未知消息类型
    """
    msg_type = data.get("type")

    if msg_type == MessageType.AUTH:
        return AuthMessage(**data)
    elif msg_type == MessageType.AUTH_OK:
        return AuthOkMessage(**data)
    elif msg_type == MessageType.AUTH_ERROR:
        return AuthErrorMessage(**data)
    elif msg_type == MessageType.REQUEST:
        return TunnelRequest(**data)
    elif msg_type == MessageType.RESPONSE:
        return TunnelResponse(**data)
    elif msg_type == MessageType.PING:
        return PingMessage(**data)
    elif msg_type == MessageType.PONG:
        return PongMessage(**data)
    else:
        raise ValueError(f"Unknown message type: {msg_type}")
