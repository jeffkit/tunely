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

import json
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
    client_version: str = Field(
        default="unknown",
        description="客户端版本（服务端记录用于升级核对；客户端应显式传真实版本，"
        "历史默认值 0.1.0 是假值，0.7.2 起改为 unknown）",
    )
    force: bool = Field(default=False, description="是否强制抢占已有连接")
    capabilities: list[str] = Field(
        default_factory=list,
        description="客户端支持的能力（协议 v2 协商）；缺字段/空数组 = 不声明任何能力。"
        "铁律：客户端只许声明自己已实现的能力",
    )


class AuthOkMessage(BaseModel):
    """认证成功响应"""

    type: MessageType = MessageType.AUTH_OK
    domain: str = Field(..., description="分配的域名")
    tunnel_id: str = Field(..., description="隧道 ID")
    server_version: str = Field(
        default="0.1.0",
        description="服务端版本（服务端发送时必填真实版本；默认值仅为兼容旧客户端解析）",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="协商启用的能力 = 服务端注册表 ∩ 客户端声明 ∩ 未被 kill 开关禁用；"
        "缺字段按空集合解析（协议 v2，无能力时服务端可省略）",
    )


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
    timeout: float = Field(default=1800.0, description="超时时间（秒）")

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


# ============== 数据面快速路径（0.7.3 去 pydantic 化） ==============
#
# 每条 TCP/SSE 数据块、每请求的 request/response 是纯热路径，pydantic 的
# 校验 + model_dump_json 在 64KB 块 × 千块/秒的量级下是纯 CPU 税。这里提供
# 手工 dict 构造（键集与 pydantic model_dump 逐字段一致，wire 不变）与
# parse_message_fast 轻校验解析（类型不对抛 ValueError，调用方按畸形消息
# 丢弃，语义同 F10）。控制面消息（auth/audit 等）照旧走 parse_message 全校验。

_MSG_TYPES = MessageType


def _now_iso() -> str:
    return datetime.now().isoformat()


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _require_str(data: dict, key: str) -> str:
    v = data.get(key)
    if not isinstance(v, str):
        raise ValueError(f"invalid message: {key} must be str")
    return v


def _opt_dict(data: dict, key: str) -> dict:
    v = data.get(key)
    if v is not None and not isinstance(v, dict):
        raise ValueError(f"invalid message: {key} must be dict")
    return v if v is not None else {}


def dump_payload(payload: dict) -> str:
    """payload dict → 紧凑 JSON（与 pydantic model_dump_json 的无空格输出一致，
    wire 体积最小；键序无关紧要但形状必须逐字段一致）"""
    return json.dumps(payload, separators=(",", ":"))


def request_payload(
    request_id: str,
    method: str,
    path: str,
    headers: dict[str, str] | None,
    body: str | None,
    timeout: float,
) -> dict:
    """TunnelRequest 的手工 dict 形状（键集与 model_dump 一致）"""
    return {
        "type": _MSG_TYPES.REQUEST.value,
        "id": request_id,
        "method": method,
        "path": path,
        "headers": headers or {},
        "body": body,
        "timeout": timeout,
        "timestamp": _now_iso(),
    }


def response_payload(
    response_id: str,
    status: int,
    headers: dict[str, str] | None,
    body: str | None,
    error: str | None = None,
    duration_ms: int = 0,
) -> dict:
    """TunnelResponse 的手工 dict 形状"""
    return {
        "type": _MSG_TYPES.RESPONSE.value,
        "id": response_id,
        "status": status,
        "headers": headers or {},
        "body": body,
        "error": error,
        "duration_ms": duration_ms,
        "timestamp": _now_iso(),
    }


def stream_start_payload(
    request_id: str, status: int, headers: dict[str, str] | None
) -> dict:
    return {
        "type": _MSG_TYPES.STREAM_START.value,
        "id": request_id,
        "status": status,
        "headers": headers or {},
        "timestamp": _now_iso(),
    }


def stream_chunk_payload(request_id: str, data: str, sequence: int) -> dict:
    return {
        "type": _MSG_TYPES.STREAM_CHUNK.value,
        "id": request_id,
        "data": data,
        "sequence": sequence,
        "timestamp": _now_iso(),
    }


def stream_end_payload(
    request_id: str,
    error: str | None,
    duration_ms: int,
    total_chunks: int,
) -> dict:
    return {
        "type": _MSG_TYPES.STREAM_END.value,
        "id": request_id,
        "error": error,
        "duration_ms": duration_ms,
        "total_chunks": total_chunks,
        "timestamp": _now_iso(),
    }


def tcp_connect_payload(conn_id: str) -> dict:
    return {
        "type": _MSG_TYPES.TCP_CONNECT.value,
        "conn_id": conn_id,
        "timestamp": _now_iso(),
    }


def tcp_data_payload(conn_id: str, data: str, sequence: int) -> dict:
    return {
        "type": _MSG_TYPES.TCP_DATA.value,
        "conn_id": conn_id,
        "data": data,
        "sequence": sequence,
        "timestamp": _now_iso(),
    }


def tcp_close_payload(conn_id: str, error: str | None = None) -> dict:
    return {
        "type": _MSG_TYPES.TCP_CLOSE.value,
        "conn_id": conn_id,
        "error": error,
        "timestamp": _now_iso(),
    }


def parse_message_fast(raw: str | bytes) -> BaseModel:
    """热路径解析：json.loads 一次 + 常见消息类型轻校验直构（跳过 pydantic 校验）

    键缺失/类型不对抛 ValueError（含 JSONDecodeError），调用方按畸形消息
    丢弃即可（F10 语义不变）。冷门类型回退 parse_message 全校验。
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("invalid message: not an object")

    msg_type = data.get("type")

    if msg_type == _MSG_TYPES.RESPONSE.value:
        status = data.get("status")
        if not _is_int(status):
            raise ValueError("invalid response: status must be int")
        return TunnelResponse.model_construct(
            type=_MSG_TYPES.RESPONSE,
            id=_require_str(data, "id"),
            status=status,
            headers=_opt_dict(data, "headers"),
            body=data.get("body"),
            error=data.get("error"),
            duration_ms=data.get("duration_ms") or 0,
            timestamp=data.get("timestamp") or _now_iso(),
        )

    if msg_type == _MSG_TYPES.STREAM_CHUNK.value:
        sequence = data.get("sequence", 0)
        if not _is_int(sequence):
            raise ValueError("invalid stream_chunk: sequence must be int")
        return StreamChunkMessage.model_construct(
            type=_MSG_TYPES.STREAM_CHUNK,
            id=_require_str(data, "id"),
            data=_require_str(data, "data"),
            sequence=sequence,
            timestamp=data.get("timestamp") or _now_iso(),
        )

    if msg_type == _MSG_TYPES.STREAM_START.value:
        status = data.get("status")
        if not _is_int(status):
            raise ValueError("invalid stream_start: status must be int")
        return StreamStartMessage.model_construct(
            type=_MSG_TYPES.STREAM_START,
            id=_require_str(data, "id"),
            status=status,
            headers=_opt_dict(data, "headers"),
            timestamp=data.get("timestamp") or _now_iso(),
        )

    if msg_type == _MSG_TYPES.STREAM_END.value:
        total_chunks = data.get("total_chunks", 0)
        duration_ms = data.get("duration_ms", 0)
        if not _is_int(total_chunks) or not _is_int(duration_ms):
            raise ValueError("invalid stream_end: counters must be int")
        return StreamEndMessage.model_construct(
            type=_MSG_TYPES.STREAM_END,
            id=_require_str(data, "id"),
            error=data.get("error"),
            duration_ms=duration_ms,
            total_chunks=total_chunks,
            timestamp=data.get("timestamp") or _now_iso(),
        )

    if msg_type == _MSG_TYPES.TCP_DATA.value:
        sequence = data.get("sequence", 0)
        if not _is_int(sequence):
            raise ValueError("invalid tcp_data: sequence must be int")
        return TcpDataMessage.model_construct(
            type=_MSG_TYPES.TCP_DATA,
            conn_id=_require_str(data, "conn_id"),
            data=_require_str(data, "data"),
            sequence=sequence,
            timestamp=data.get("timestamp") or _now_iso(),
        )

    if msg_type == _MSG_TYPES.TCP_CLOSE.value:
        return TcpCloseMessage.model_construct(
            type=_MSG_TYPES.TCP_CLOSE,
            conn_id=_require_str(data, "conn_id"),
            error=data.get("error"),
            timestamp=data.get("timestamp") or _now_iso(),
        )

    if msg_type == _MSG_TYPES.TCP_CONNECT.value:
        return TcpConnectMessage.model_construct(
            type=_MSG_TYPES.TCP_CONNECT,
            conn_id=_require_str(data, "conn_id"),
            timestamp=data.get("timestamp") or _now_iso(),
        )

    if msg_type == _MSG_TYPES.REQUEST.value:
        timeout = data.get("timeout", 1800.0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("invalid request: timeout must be number")
        return TunnelRequest.model_construct(
            type=_MSG_TYPES.REQUEST,
            id=_require_str(data, "id"),
            method=_require_str(data, "method"),
            path=_require_str(data, "path"),
            headers=_opt_dict(data, "headers"),
            body=data.get("body"),
            timeout=timeout,
            timestamp=data.get("timestamp") or _now_iso(),
        )

    if msg_type == _MSG_TYPES.PING.value:
        return PingMessage.model_construct(
            type=_MSG_TYPES.PING, timestamp=data.get("timestamp") or _now_iso()
        )

    if msg_type == _MSG_TYPES.PONG.value:
        return PongMessage.model_construct(
            type=_MSG_TYPES.PONG, timestamp=data.get("timestamp") or _now_iso()
        )

    # 冷门/未知类型：全量 pydantic 校验（ValidationError ⊂ ValueError，F10 兼容）
    return parse_message(data)
