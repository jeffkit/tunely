"""
WS-Tunnel 协议定义

协议版本: 1.1 (SSE 支持)

消息类型:
- auth: 客户端认证请求
- auth_ok: 服务端认证成功响应
- auth_error: 服务端认证失败响应
- request: 服务端发送的 HTTP 请求
- response: 客户端返回的 HTTP 响应 (完整响应)
- stream_start: 流式响应开始
- stream_chunk: 流式响应数据块
- stream_end: 流式响应结束
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

    # 请求-响应（HTTP 模式）
    REQUEST = "request"
    RESPONSE = "response"

    # 流式响应（SSE 支持）
    STREAM_START = "stream_start"
    STREAM_CHUNK = "stream_chunk"
    STREAM_END = "stream_end"

    # TCP 模式
    TCP_CONNECT = "tcp_connect"  # 服务端通知新 TCP 连接
    TCP_DATA = "tcp_data"        # TCP 数据传输（双向）
    TCP_CLOSE = "tcp_close"      # TCP 连接关闭

    # 心跳
    PING = "ping"
    PONG = "pong"


# ============== 认证消息 ==============


class AuthMessage(BaseModel):
    """客户端认证请求"""

    type: MessageType = MessageType.AUTH
    token: str = Field(..., description="隧道令牌")
    client_version: str = Field(default="0.1.0", description="客户端版本")
    force: bool = Field(default=False, description="是否强制抢占已有连接")


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


# ============== 流式响应消息（SSE 支持） ==============


class StreamStartMessage(BaseModel):
    """
    流式响应开始（客户端 → 服务端）
    
    当检测到 SSE 响应（Content-Type: text/event-stream）时发送
    """

    type: MessageType = MessageType.STREAM_START
    id: str = Field(..., description="请求 ID，与 TunnelRequest.id 对应")
    status: int = Field(..., description="HTTP 状态码")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP 响应头")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="开始时间"
    )


class StreamChunkMessage(BaseModel):
    """
    流式响应数据块（客户端 → 服务端）
    
    包含一个 SSE 数据块
    """

    type: MessageType = MessageType.STREAM_CHUNK
    id: str = Field(..., description="请求 ID，与 TunnelRequest.id 对应")
    data: str = Field(..., description="数据块内容")
    sequence: int = Field(default=0, description="数据块序号，从 0 开始")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="发送时间"
    )


class StreamEndMessage(BaseModel):
    """
    流式响应结束（客户端 → 服务端）
    
    表示 SSE 流已结束
    """

    type: MessageType = MessageType.STREAM_END
    id: str = Field(..., description="请求 ID，与 TunnelRequest.id 对应")
    error: str | None = Field(default=None, description="错误信息（如果异常结束）")
    duration_ms: int = Field(default=0, description="总耗时（毫秒）")
    total_chunks: int = Field(default=0, description="总数据块数")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="结束时间"
    )


# ============== TCP 模式消息 ==============


class TcpConnectMessage(BaseModel):
    """
    TCP 连接建立（服务端 → 客户端）
    
    当有新的 TCP 连接到达时，服务端发送此消息通知客户端
    """

    type: MessageType = MessageType.TCP_CONNECT
    conn_id: str = Field(..., description="连接唯一 ID")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="连接时间"
    )


class TcpDataMessage(BaseModel):
    """
    TCP 数据传输（双向）
    
    用于在服务端和客户端之间传输原始 TCP 数据
    """

    type: MessageType = MessageType.TCP_DATA
    conn_id: str = Field(..., description="连接 ID")
    data: str = Field(..., description="Base64 编码的二进制数据")
    sequence: int = Field(default=0, description="数据包序号")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="发送时间"
    )


class TcpCloseMessage(BaseModel):
    """
    TCP 连接关闭（双向）
    
    通知对方关闭 TCP 连接
    """

    type: MessageType = MessageType.TCP_CLOSE
    conn_id: str = Field(..., description="连接 ID")
    error: str | None = Field(default=None, description="错误信息（如果异常关闭）")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(), description="关闭时间"
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
    elif msg_type == MessageType.STREAM_START:
        return StreamStartMessage(**data)
    elif msg_type == MessageType.STREAM_CHUNK:
        return StreamChunkMessage(**data)
    elif msg_type == MessageType.STREAM_END:
        return StreamEndMessage(**data)
    elif msg_type == MessageType.TCP_CONNECT:
        return TcpConnectMessage(**data)
    elif msg_type == MessageType.TCP_DATA:
        return TcpDataMessage(**data)
    elif msg_type == MessageType.TCP_CLOSE:
        return TcpCloseMessage(**data)
    elif msg_type == MessageType.PING:
        return PingMessage(**data)
    elif msg_type == MessageType.PONG:
        return PongMessage(**data)
    else:
        raise ValueError(f"Unknown message type: {msg_type}")
