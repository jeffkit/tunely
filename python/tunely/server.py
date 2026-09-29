"""
WS-Tunnel 服务端 SDK

提供 WebSocket 隧道服务端功能，可嵌入到 FastAPI 应用中

使用示例:
    from fastapi import FastAPI
    from tunely import TunnelServer

    app = FastAPI()
    tunnel_server = TunnelServer(database_url="sqlite+aiosqlite:///./tunnels.db")

    # 注册路由
    app.include_router(tunnel_server.router)

    # 在应用启动时初始化
    @app.on_event("startup")
    async def startup():
        await tunnel_server.initialize()

    # 转发请求
    response = await tunnel_server.forward(
        domain="agent-001",
        method="POST",
        path="/api/chat",
        body={"message": "hello"}
    )
"""

import asyncio
import errno
import hashlib
import hmac
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Literal

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict

from .config import TunnelServerConfig
from .database import DatabaseManager
from .models import Tunnel
from .protocol import (
    AuthErrorMessage,
    AuthMessage,
    AuthOkMessage,
    MessageType,
    PingMessage,
    PongMessage,
    TunnelRequest,
    TunnelResponse,
    StreamStartMessage,
    StreamChunkMessage,
    StreamEndMessage,
    TcpConnectMessage,
    TcpDataMessage,
    TcpCloseMessage,
    UdpCloseMessage,
    UdpOpenMessage,
    parse_message,
    parse_message_fast,
    dump_payload,
    request_payload,
    tcp_close_payload,
    tcp_connect_payload,
    tcp_data_payload,
    udp_close_payload,
    udp_open_payload,
    decode_tcp_data_frame,
    decode_udp_data_frame,
    encode_tcp_data_frame,
    encode_udp_data_frame,
    FRAME_TYPE_UDP_DATA,
)
from .repository import TunnelRepository, TunnelRequestLogRepository, AdminAuditLogRepository

logger = logging.getLogger(__name__)

# 认证失败断开前的延迟（秒）：提高 token 暴力尝试成本（仅认证失败路径，
# 业务拒绝路径如 disabled / already connected 不受影响）
_AUTH_FAILURE_DELAY = 1.0

# 流量统计落库周期（秒）：把内存计数增量累加写回 tunnels 表
_BYTES_FLUSH_INTERVAL = 30.0

# 请求日志后台写队列长度：写库速度跟不上转发时最多积压这么多条，
# 再多就丢弃并计数（日志是尽力而为的观测数据，不能反过来堵转发面）
_LOG_QUEUE_MAXSIZE = 1000

# 请求日志保留清理周期（秒）
_LOG_RETENTION_SWEEP_INTERVAL = 3600.0

# 响应体超过该长度时跳过日志归一化（json.dumps(json.loads(...))），
# 直接截原始前缀——全量 parse+dump 只为取前 1 万字符是纯开销
_LOG_NORMALIZE_MAX_BODY = 1_000_000

# 每条外部 TCP 连接的写队列长度（条；每条 ≤64KB，64 条 ≈ 4MB 缓冲）。
# 写循环独立消费，慢接收方只堵自己这条连接，不阻塞隧道 WS 消息循环
_TCP_WRITE_QUEUE_MAXSIZE = 64

# 流式请求的失败哨兵：经队列唤醒消费侧（0.7.3 起消费侧单 await queue.get()，
# 不再每 chunk 双 task 等待「新消息 vs 失败事件」）
_STREAM_FAILED = object()

# 每个 UDP 监听器的收包队列长度（个；datagram_received 是同步回调不能 await，
# 入队后由专职 pump 任务顺序消费）。队满按丢包处理（UDP 有损语义），不反压内核
_UDP_QUEUE_MAXSIZE = 1024

# 服务端能力注册表（协议 v2 能力协商，见 docs/PROTOCOL_V2.md §0）。
# 认证时按「服务端注册表 ∩ 客户端声明 − disable_capabilities」回交集。
# T2 binary_frames：WS binary 数据面帧；T3 chunked_http：非 SSE 大响应流式回传；
# T4 udp：UDP 会话透传（0x03 二进制帧数据面 + udp_open/udp_close JSON 控制面）。
SERVER_CAPABILITIES: list[str] = ["binary_frames", "chunked_http", "udp"]


def _client_ip(http_request: Request | None) -> str | None:
    """尽力而为地提取客户端 IP（拿不到返回 None，不抛异常）"""
    try:
        if http_request is not None and http_request.client:
            host = http_request.client.host
            if isinstance(host, str) and host:
                return host
    except Exception:
        pass
    return None


def _ws_client_ip(websocket: WebSocket | None) -> str | None:
    """尽力而为地提取 WebSocket 客户端 IP（mock/测试环境下拿不到返回 None）"""
    try:
        client = getattr(websocket, "client", None) if websocket is not None else None
        if client is not None:
            host = getattr(client, "host", None)
            if isinstance(host, str) and host:
                return host
    except Exception:
        pass
    return None


def is_valid_forward_path(path: str) -> bool:
    """
    校验转发路径合法性（@-SSRF 防护）

    path 会被直接拼进客户端侧的 target_url + path。若不以 "/" 开头，
    攻击者可传 "@169.254.169.254/" 之类的值，客户端拼出
    "http://127.0.0.1:3080@169.254.169.254/..."，host 被改写为攻击者目标，
    形成内网 SSRF。合法 path 必须以 "/" 开头。
    """
    return isinstance(path, str) and path.startswith("/")


def normalize_forward_path(path: str) -> str:
    """归一化转发路径：缺 "/" 前缀时补上（浏览器 /t/ 路由对空/相对路径容错）"""
    return path if is_valid_forward_path(path) else f"/{path}"


# ============== 数据结构 ==============


@dataclass
class ActiveConnection:
    """活跃的隧道连接"""

    websocket: WebSocket
    tunnel_id: int
    domain: str
    token: str
    # 隧道转发模式（注册时从 DB 行读取一次缓存；forward 路由用，避免每请求查库。
    # token 轮换/隧道删除后连接仍存活时，沿用缓存值，行为兼容）
    mode: str = "http"
    # 客户端自报版本（AuthMessage.client_version；缺省 unknown）。
    # 用于升级前核对现网客户端版本分布（ROLLING_UPGRADE.md 0.7.2）。
    # 注意：历史 TS/py 客户端曾硬编码/默认 "0.1.0"，该值不可信到 0.2.7/0.7.2 前
    client_version: str = "unknown"
    # 协商启用的能力（AuthOk.capabilities 的连接侧快照，协议 v2）。
    # 数据面路径按「该连接是否协商了 X」门控：binary_frames（T2）、
    # chunked_http（T3）、udp（T4）。
    capabilities: frozenset[str] = field(default_factory=frozenset)
    connected_at: datetime = field(default_factory=datetime.now)
    last_heartbeat: datetime = field(default_factory=datetime.now)


@dataclass
class PendingRequest:
    """待响应的请求（普通响应）"""

    request_id: str
    future: asyncio.Future
    domain: str = ""  # 归属隧道（跨隧道串扰校验用）
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class PendingStreamRequest:
    """待响应的流式请求（SSE 支持）"""

    request_id: str
    queue: asyncio.Queue  # 存储流式数据块（有界：stream_queue_maxsize，0 = 不限）
    started: bool = False
    ended: bool = False
    start_message: StreamStartMessage | None = None
    end_message: StreamEndMessage | None = None
    domain: str = ""  # 归属隧道（跨隧道串扰校验用）
    # 流错误通道：队列写满 / 隧道断连时置 error 并 set 事件，
    # 消费侧（forward_stream）立即以错误结束，不依赖超时兜底
    error: str | None = None
    failed: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class TcpConnectionState:
    """TCP 连接状态（服务端有真实 TCP 连接时使用）"""

    conn_id: str
    domain: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    read_task: asyncio.Task | None = None
    # WS→外部 TCP 方向的写队列：写循环独立消费（write_task），
    # 队满按「杀这条 TCP 连接」处理，不反压隧道 WS 消息循环
    write_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=_TCP_WRITE_QUEUE_MAXSIZE)
    )
    write_task: asyncio.Task | None = None
    websocket: WebSocket | None = None
    created_at: datetime = field(default_factory=datetime.now)
    closed: bool = False


@dataclass
class PendingTcpRequest:
    """待响应的 TCP 请求（HTTP 触发的 TCP 转发）

    当 HTTP 请求触发 TCP 隧道转发时，服务端没有真实的 TCP 连接，
    而是通过 WebSocket 与客户端交换 TCP 数据。

    工作流:
    1. _forward_tcp 创建 PendingTcpRequest（含 Future）
    2. 发送 TcpConnectMessage + TcpDataMessage 给客户端
    3. 客户端建立到目标的真实 TCP 连接，转发数据
    4. 客户端返回 TcpDataMessage（目标的响应数据）→ 累积到 chunks
    5. 客户端返回 TcpCloseMessage → 解析 Future
    6. _forward_tcp 收到 Future 结果，返回累积的数据
    """

    conn_id: str
    future: asyncio.Future
    chunks: list[bytes] = field(default_factory=list)
    total_bytes: int = 0  # 已累积字节数（tcp_forward_max_buffer_bytes 限额用）
    domain: str = ""  # 归属隧道（跨隧道串扰校验用）
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class UdpSessionState:
    """UDP 会话状态（服务端监听场景，协议 v2 udp）

    会话键 = (监听端口, 外部 addr)：同一外部地址在同一监听端口上的所有
    数据报归属同一会话（UDP 无连接，服务端按源地址区分「连接」）。
    """

    session_id: str
    domain: str  # 归属隧道（跨隧道串扰校验 + 回程连接查找用）
    port: int  # 服务端 UDP 监听端口
    addr: tuple  # 外部源地址 (host, port)，回程 sendto 目标
    created_at: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)  # 空闲超时回收用


# ============== 请求/响应模型 ==============


class CreateTunnelRequest(BaseModel):
    """创建隧道请求"""

    # extra="forbid"：服务端未实现的字段直接报错，而不是静默丢弃（issue #2）
    model_config = ConfigDict(extra="forbid")

    domain: str
    name: str | None = None
    description: str | None = None
    mode: Literal["http", "tcp"] = "http"


class CreateTunnelResponse(BaseModel):
    """创建隧道响应"""

    domain: str
    token: str
    name: str | None = None
    mode: str = "http"


class CheckAvailabilityResponse(BaseModel):
    """检查名称可用性响应"""

    available: bool
    name: str
    reason: str | None = None


class TunnelInfo(BaseModel):
    """隧道信息"""

    domain: str
    name: str | None = None
    description: str | None = None
    mode: str = "http"
    enabled: bool
    bytes_in: int = 0
    bytes_out: int = 0
    connected: bool
    token: str | None = None  # 可选，仅在需要时返回
    created_at: str | None = None
    last_connected_at: str | None = None
    total_requests: int = 0
    # 当前连接客户端的自报版本（未连接时 None；历史客户端可能报假值 0.1.0）
    client_version: str | None = None


class UpdateTunnelRequest(BaseModel):
    """更新隧道请求"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    mode: Literal["http", "tcp"] | None = None


class RegenerateTokenResponse(BaseModel):
    """重新生成 Token 响应"""

    domain: str
    token: str


class ForwardRequest(BaseModel):
    """转发请求"""

    method: str = "POST"
    path: str = "/"
    headers: dict[str, str] = {}
    body: Any = None
    timeout: float = 1800.0


class ForwardResponse(BaseModel):
    """转发响应"""

    status: int
    headers: dict[str, str] = {}
    body: Any = None
    duration_ms: int = 0
    error: str | None = None


# ============== 隧道管理器 ==============


class TunnelManager:
    """
    隧道管理器

    管理所有活跃的隧道连接和待响应的请求
    """

    def __init__(
        self,
        tcp_forward_max_buffer_bytes: int = 10485760,
        stream_queue_maxsize: int = 1024,
    ):
        # token → ActiveConnection
        self._connections: dict[str, ActiveConnection] = {}

        # domain → token（缓存，用于快速查找）
        self._domain_token_map: dict[str, str] = {}

        # request_id → PendingRequest（普通响应）
        self._pending_requests: dict[str, PendingRequest] = {}

        # request_id → PendingStreamRequest（流式响应/SSE）
        self._pending_stream_requests: dict[str, PendingStreamRequest] = {}

        # conn_id → TcpConnectionState（TCP 模式 - 服务端有真实 TCP 连接）
        self._tcp_connections: dict[str, TcpConnectionState] = {}

        # conn_id → PendingTcpRequest（TCP 模式 - HTTP 触发的 TCP 转发）
        self._pending_tcp_requests: dict[str, PendingTcpRequest] = {}

        # (监听端口, 外部 addr) → UdpSessionState（UDP 模式，协议 v2 udp）
        self._udp_sessions: dict[tuple, UdpSessionState] = {}

        # 内存安全上限（0 = 不限制）
        self.tcp_forward_max_buffer_bytes = tcp_forward_max_buffer_bytes
        self.stream_queue_maxsize = stream_queue_maxsize

        # TCP 写循环收尾任务（remove 后异步等队列发完，不阻塞调用方）
        self._reapers: set[asyncio.Task] = set()

        self._lock = asyncio.Lock()

    async def register(
        self,
        websocket: WebSocket,
        tunnel_id: int,
        domain: str,
        token: str,
        force: bool = False,
        mode: str = "http",
        client_version: str = "unknown",
        capabilities: frozenset[str] = frozenset(),
    ) -> tuple[bool, str | None]:
        """
        注册隧道连接

        Args:
            websocket: WebSocket 连接
            tunnel_id: 隧道 ID
            domain: 隧道域名
            token: 隧道令牌
            force: 是否强制抢占已有连接
            mode: 隧道转发模式（http/tcp，注册时从 DB 读一次缓存，forward 路由用）
            client_version: 客户端自报版本（AuthMessage.client_version）
            capabilities: 协商启用的能力（协议 v2，AuthOk 回的交集）

        Returns:
            (success, error_message) - 成功返回 (True, None)，失败返回 (False, error_message)
        """
        async with self._lock:
            # 检查是否已有连接
            if token in self._connections:
                old_conn = self._connections[token]
                
                # 检查旧连接是否健康（通过检查 WebSocket 状态）
                try:
                    is_healthy = old_conn.websocket.client_state.name == "CONNECTED"
                except Exception:
                    is_healthy = False
                
                if is_healthy and not force:
                    seconds_since_heartbeat = (datetime.now() - old_conn.last_heartbeat).total_seconds()
                    
                    if seconds_since_heartbeat < 120:
                        logger.warning(f"拒绝新连接: domain={domain}，已有活跃连接 (上次心跳 {seconds_since_heartbeat:.0f}s 前)")
                        return (False, f"已有活跃连接存在，使用 --force 参数可强制抢占")
                    else:
                        logger.info(f"旧连接可能已过期 (上次心跳 {seconds_since_heartbeat:.0f}s 前)，自动替换: domain={domain}")
                        force = True
                
                # 关闭旧连接（不健康或强制抢占或过期）
                try:
                    await old_conn.websocket.close(code=1000, reason="New connection (force)" if force else "Connection replaced")
                except Exception:
                    pass
                logger.info(f"关闭旧连接: domain={domain}, force={force}")

            conn = ActiveConnection(
                websocket=websocket,
                tunnel_id=tunnel_id,
                domain=domain,
                token=token,
                mode=mode if mode in ("http", "tcp") else "http",
                client_version=client_version or "unknown",
                capabilities=frozenset(capabilities or ()),
            )
            self._connections[token] = conn
            self._domain_token_map[domain] = token

            logger.info(f"隧道已连接: domain={domain}, mode={conn.mode}")
            return (True, None)

    async def unregister(self, token: str, websocket=None) -> None:
        """注销隧道连接

        websocket 传入当前退出处理的连接时做身份校验：force 抢占后注册表
        已指向新连接，旧连接的清理不得误删新注册（0.6.1 回归修复）。

        0.7.0 起：注销时立刻失败该连接的在途 pending（普通请求 / 流式 /
        HTTP 触发的 TCP 转发），避免断连后请求悬挂至超时。注意不触碰
        _tcp_connections（服务端监听的真实 TCP 连接由自身生命周期管理）。
        """
        async with self._lock:
            conn = self._connections.get(token)
            if conn is None:
                return
            if websocket is not None and conn.websocket is not websocket:
                logger.info(
                    f"跳过注销: token={token} 已被新连接接管 (domain={conn.domain})"
                )
                return
            self._connections.pop(token, None)
            self._domain_token_map.pop(conn.domain, None)
            logger.info(f"隧道已断开: domain={conn.domain}")
            self._fail_pending_requests_locked(conn.domain)

    def _fail_pending_requests_locked(self, domain: str) -> None:
        """失败指定域名的全部在途 pending（调用方须持有 self._lock）

        仅匹配记录了归属域名的条目；_tcp_connections（真实 TCP 连接）
        不在此清理，由连接自身生命周期管理。
        """
        if not domain:
            return

        # 1. 普通请求：future 以 ConnectionError 结束（forward 捕获后返回 500）
        for request_id in [
            rid
            for rid, pending in self._pending_requests.items()
            if pending.domain == domain
        ]:
            pending = self._pending_requests.pop(request_id, None)
            if pending and not pending.future.done():
                pending.future.set_exception(
                    ConnectionError("tunnel disconnected")
                )
                logger.warning(
                    f"隧道断连，在途请求已失败: domain={domain}, request_id={request_id}"
                )

        # 2. 流式请求：哨兵入队让消费侧立即以错误结束
        for request_id in [
            rid
            for rid, pending in self._pending_stream_requests.items()
            if pending.domain == domain
        ]:
            pending = self._pending_stream_requests.pop(request_id, None)
            if pending:
                pending.error = "tunnel disconnected"
                pending.ended = True
                self._signal_stream_failed(pending)
                logger.warning(
                    f"隧道断连，流式请求已终止: domain={domain}, request_id={request_id}"
                )

        # 3. HTTP 触发的 TCP 转发：以错误完成 future（forward 返回 502）
        for conn_id in [
            cid
            for cid, pending in self._pending_tcp_requests.items()
            if pending.domain == domain
        ]:
            pending = self._pending_tcp_requests.pop(conn_id, None)
            if pending and not pending.future.done():
                pending.future.set_result(
                    {"error": "tunnel disconnected", "data": b""}
                )
                logger.warning(
                    f"隧道断连，TCP 转发请求已失败: domain={domain}, conn_id={conn_id}"
                )

        # 4. UDP 会话：直接清表（协议 v2 udp）。WS 已断，udp_close 发不到客户端，
        # 客户端靠自身 WS 断连清理路径回收本地 socket。
        removed = self.cleanup_udp_sessions_for_domain(domain)
        if removed:
            logger.warning(
                f"隧道断连，UDP 会话已清理: domain={domain}, sessions={removed}"
            )

    def get_connection_by_domain(self, domain: str) -> ActiveConnection | None:
        """根据域名获取连接"""
        token = self._domain_token_map.get(domain)
        if token:
            return self._connections.get(token)
        return None

    def get_connection_by_token(self, token: str) -> ActiveConnection | None:
        """根据令牌获取连接"""
        return self._connections.get(token)

    def is_connected(self, domain: str) -> bool:
        """检查域名是否已连接"""
        return domain in self._domain_token_map

    def get_client_version(self, domain: str) -> str | None:
        """获取隧道的客户端自报版本（未连接返回 None）"""
        conn = self.get_connection_by_domain(domain)
        return conn.client_version if conn else None

    def list_connected_domains(self) -> list[str]:
        """列出所有已连接的域名"""
        return list(self._domain_token_map.keys())

    async def create_pending_request(self, request_id: str, domain: str = "") -> asyncio.Future:
        """创建待响应的请求（普通响应）"""
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = PendingRequest(
            request_id=request_id,
            future=future,
            domain=domain,
        )
        return future

    def get_pending_request_domain(self, request_id: str) -> str | None:
        """查询待响应请求的归属隧道（不存在返回 None；跨隧道串扰校验用）"""
        pending = self._pending_requests.get(request_id)
        return pending.domain if pending else None

    def pending_requests_count(self) -> int:
        """当前待响应请求数（限额用）"""
        return len(self._pending_requests)

    async def complete_request(self, request_id: str, response: TunnelResponse) -> bool:
        """完成请求（普通响应）"""
        pending = self._pending_requests.pop(request_id, None)
        if pending and not pending.future.done():
            pending.future.set_result(response)
            return True
        return False

    async def fail_request(self, request_id: str, error: str) -> bool:
        """请求失败"""
        pending = self._pending_requests.pop(request_id, None)
        if pending and not pending.future.done():
            pending.future.set_exception(Exception(error))
            return True
        # 也检查流式请求
        stream_pending = self._pending_stream_requests.get(request_id)
        if stream_pending:
            await self.fail_stream_request(request_id, error)
            return True
        return False

    async def update_heartbeat(self, token: str) -> None:
        """更新心跳时间"""
        conn = self._connections.get(token)
        if conn:
            conn.last_heartbeat = datetime.now()

    # ============== 流式请求支持（SSE） ==============

    def _stream_queue_put(self, pending: PendingStreamRequest, message) -> bool:
        """向流式队列写入一条消息（非阻塞）

        队列写满（消费侧过慢）时按流错误处理：终止该流，避免阻塞
        WebSocket 消息循环或无界积压内存。返回 False 表示写入失败。
        """
        try:
            pending.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            logger.warning(
                f"流式队列已满 (maxsize={pending.queue.maxsize})，按流错误结束: "
                f"request_id={pending.request_id}"
            )
            pending.error = "stream queue overflow"
            pending.ended = True
            self._signal_stream_failed(pending)
            return False

    def _signal_stream_failed(self, pending: PendingStreamRequest) -> None:
        """经队列哨兵唤醒消费侧：流已失败（溢出/断连/显式失败）

        队列满时丢弃一条滞留 chunk 腾位（同步代码段无 await，get_nowait
        必然成功）；消费侧读到哨兵即以 pending.error 结束，不等超时兜底。
        """
        pending.failed.set()
        attempts = (pending.queue.maxsize or 1) + 1
        for _ in range(attempts):
            try:
                pending.queue.put_nowait(_STREAM_FAILED)
                return
            except asyncio.QueueFull:
                try:
                    pending.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

    async def create_stream_request(self, request_id: str, domain: str = "") -> PendingStreamRequest:
        """创建待响应的流式请求"""
        maxsize = self.stream_queue_maxsize if self.stream_queue_maxsize > 0 else 0
        pending = PendingStreamRequest(
            request_id=request_id,
            queue=asyncio.Queue(maxsize=maxsize),
            domain=domain,
        )
        self._pending_stream_requests[request_id] = pending
        return pending

    async def fail_stream_request(self, request_id: str, error: str) -> bool:
        """以错误终止流式请求（哨兵唤醒消费侧，不必等超时兜底）"""
        pending = self._pending_stream_requests.get(request_id)
        if pending:
            pending.error = error
            pending.ended = True
            self._signal_stream_failed(pending)
            self._pending_stream_requests.pop(request_id, None)
            return True
        return False

    def get_pending_stream_domain(self, request_id: str) -> str | None:
        """查询流式请求的归属隧道（不存在返回 None；跨隧道串扰校验用）"""
        pending = self._pending_stream_requests.get(request_id)
        return pending.domain if pending else None

    async def handle_stream_start(self, message: StreamStartMessage) -> bool:
        """处理流式响应开始"""
        pending = self._pending_stream_requests.get(message.id)
        if pending:
            pending.started = True
            pending.start_message = message
            return self._stream_queue_put(pending, message)
        return False

    async def handle_stream_chunk(self, message: StreamChunkMessage) -> bool:
        """处理流式数据块"""
        pending = self._pending_stream_requests.get(message.id)
        if pending and pending.started and not pending.ended:
            return self._stream_queue_put(pending, message)
        return False

    async def handle_stream_end(self, message: StreamEndMessage) -> bool:
        """处理流式响应结束"""
        pending = self._pending_stream_requests.get(message.id)
        if pending:
            pending.ended = True
            pending.end_message = message
            ok = self._stream_queue_put(pending, message)
            if ok:
                # None 哨兵：消费侧读到即认为流结束。队列满时上面已按错误终止，
                # 无需再放哨兵。
                self._stream_queue_put(pending, None)  # 发送结束信号
            # 注意：不立即删除，等迭代器完成后再清理
            return ok
        return False

    async def cleanup_stream_request(self, request_id: str) -> None:
        """清理流式请求"""
        self._pending_stream_requests.pop(request_id, None)

    async def bridge_response_to_stream(self, response: TunnelResponse) -> bool:
        """协议 v2 chunked_http（T3）补桥：TunnelResponse → 流式三段消息

        客户端对非 SSE 目标只回 TunnelResponse（缓冲形态）。此前该响应只会
        完成缓冲型 future（_pending_requests），forward_stream 的流式消费侧
        干等到超时——docstring 承诺的「完整响应 SingleChunk」从未实现。

        现在：若 id 是 forward_stream 发出的流式 pending（stream_ok 请求），
        把完整响应合成为 StreamStart + StreamChunk(全量 body, plain) +
        StreamEnd 依次经 _stream_queue_put 投入该流队列并清理（收尾语义参照
        handle_stream_end：置 started/ended 标记 + None 哨兵唤醒消费侧）。
        某段队列写满时 _stream_queue_put 已按流错误终止该流，后续段不再投递。

        返回 True 表示已按流式处理；False 表示不是流式请求（调用方走缓冲
        完成路径，/forward 与 /t/ 缓冲分支行为不变）。
        """
        pending = self._pending_stream_requests.get(response.id)
        if pending is None:
            return False

        start = StreamStartMessage(
            id=response.id, status=response.status, headers=response.headers
        )
        chunk = None
        if response.body is not None:
            chunk = StreamChunkMessage(
                id=response.id, data=response.body, sequence=0, encoding="plain"
            )
        end = StreamEndMessage(
            id=response.id,
            error=response.error,
            duration_ms=response.duration_ms,
            total_chunks=1 if chunk is not None else 0,
        )

        ok = self._stream_queue_put(pending, start)
        if ok and chunk is not None:
            ok = self._stream_queue_put(pending, chunk)
        if ok:
            ok = self._stream_queue_put(pending, end)
        if ok:
            # None 哨兵：消费侧读到即认为流结束（与 handle_stream_end 一致）
            self._stream_queue_put(pending, None)

        # 收尾标记（参照 handle_stream_start/end 的字段语义），并立即清理
        # pending——合成流已完整，不再接受后续同名流消息。
        # started 仅在入队成功时置位（forward_stream 以此决定是否计请求数）
        pending.started = ok
        pending.start_message = start
        pending.ended = True
        pending.end_message = end
        self._pending_stream_requests.pop(response.id, None)
        return True

    # ============== TCP 模式支持 ==============

    async def register_tcp_connection(
        self,
        conn_id: str,
        domain: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        websocket: WebSocket,
    ) -> None:
        """注册 TCP 连接（同时启动其独立写循环）"""
        tcp_conn = TcpConnectionState(
            conn_id=conn_id,
            domain=domain,
            reader=reader,
            writer=writer,
            websocket=websocket,
        )
        self._tcp_connections[conn_id] = tcp_conn
        tcp_conn.write_task = asyncio.create_task(self._tcp_write_loop(tcp_conn))
        logger.info(f"注册 TCP 连接: {conn_id} for domain={domain}")

    async def get_tcp_connection(self, conn_id: str) -> TcpConnectionState | None:
        """获取 TCP 连接"""
        return self._tcp_connections.get(conn_id)

    def count_tcp_connections(self, domain: str) -> int:
        """统计指定隧道当前活跃的外部 TCP 连接数（未 closed）"""
        return sum(
            1
            for tcp_conn in self._tcp_connections.values()
            if tcp_conn.domain == domain and not tcp_conn.closed
        )

    def list_tcp_connection_domains(self) -> list[str]:
        """列出当前有活跃外部 TCP 连接的隧道域名（去重排序）"""
        return sorted(
            {
                state.domain
                for state in self._tcp_connections.values()
                if not state.closed and state.domain
            }
        )

    async def close_all_tcp_connections(self) -> None:
        """关闭全部活跃外部 TCP 连接（服务端优雅停机时调用）"""
        states = list(self._tcp_connections.values())
        for tcp_conn in states:
            tcp_conn.closed = True
            if tcp_conn.read_task:
                tcp_conn.read_task.cancel()
            if tcp_conn.write_task:
                tcp_conn.write_task.cancel()
        for tcp_conn in states:
            if tcp_conn.writer:
                try:
                    tcp_conn.writer.close()
                    await tcp_conn.writer.wait_closed()
                except Exception:
                    pass
        self._tcp_connections.clear()
        if states:
            logger.info(f"已关闭 {len(states)} 个活跃 TCP 连接")

    async def remove_tcp_connection(self, conn_id: str) -> bool:
        """移除 TCP 连接

        写队列里的存量数据交给 reaper 异步收尾（最多再等 5s 发完），
        调用方（隧道 WS 消息循环 / 外部连接清理）不被慢接收方阻塞。

        返回 True 表示本次调用真正移除；False = 连接已不存在
        （对端先一步关闭，并发关闭竞态下的正常时序）。
        """
        tcp_conn = self._tcp_connections.pop(conn_id, None)
        if not tcp_conn:
            return False
        logger.info(f"移除 TCP 连接: {conn_id}")
        tcp_conn.closed = True
        # 取消读取任务
        if tcp_conn.read_task:
            tcp_conn.read_task.cancel()
        # 写循环：发哨兵让它发完存量再退出；队满/已结束则直接收尾
        if tcp_conn.write_task and not tcp_conn.write_task.done():
            try:
                tcp_conn.write_queue.put_nowait(None)
            except asyncio.QueueFull:
                tcp_conn.write_task.cancel()
            reaper = asyncio.create_task(self._reap_tcp_writer(tcp_conn))
            self._reapers.add(reaper)
            reaper.add_done_callback(self._reapers.discard)
        elif tcp_conn.writer and not tcp_conn.writer.is_closing():
            try:
                tcp_conn.writer.close()
            except Exception:
                pass
        return True

    async def _reap_tcp_writer(self, tcp_conn: TcpConnectionState) -> None:
        """等写循环发完存量后关闭外部连接（有界等待，超时强杀）"""
        if tcp_conn.write_task:
            try:
                await asyncio.wait_for(tcp_conn.write_task, timeout=5.0)
            except (asyncio.CancelledError, Exception):
                pass
        if tcp_conn.writer and not tcp_conn.writer.is_closing():
            try:
                tcp_conn.writer.close()
            except Exception:
                pass
        if tcp_conn.writer:
            try:
                await tcp_conn.writer.wait_closed()
            except Exception:
                pass

    async def _tcp_write_loop(self, tcp_conn: TcpConnectionState) -> None:
        """独立写循环：把 WS 侧收到的数据顺序写往外部 TCP 连接

        drain 阻塞发生在本任务内——慢接收方只拖慢自己这条连接，
        不再阻塞隧道 WS 消息循环（0.7.1 队头阻塞修复）。
        """
        try:
            while True:
                data = await tcp_conn.write_queue.get()
                if data is None:
                    break
                tcp_conn.writer.write(data)
                await tcp_conn.writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"TCP 写循环失败，关闭外部连接: conn_id={tcp_conn.conn_id}, {e}")
            tcp_conn.closed = True
        finally:
            if tcp_conn.writer and not tcp_conn.writer.is_closing():
                try:
                    tcp_conn.writer.close()
                except Exception:
                    pass

    async def handle_tcp_data(self, conn_id: str, data: bytes) -> bool:
        """处理 TCP 数据（入队写往真实 TCP 连接 - 服务端监听场景）

        非阻塞入队：写盘速度由独立写循环消化；队满（下游消费不动）
        只关这条 TCP 连接，不反压隧道 WS 消息循环。
        """
        tcp_conn = self._tcp_connections.get(conn_id)
        if not tcp_conn or tcp_conn.closed:
            # 在途数据竞到关闭之后（并发关闭竞态）属正常时序，debug 防刷屏
            logger.debug(f"TCP 连接不存在或已关闭: {conn_id}")
            return False

        try:
            tcp_conn.write_queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            logger.warning(
                f"TCP 写队列已满 ({tcp_conn.write_queue.maxsize})，关闭连接: conn_id={conn_id}"
            )
            await self.remove_tcp_connection(conn_id)
            return False

    # ============== TCP Pending Request（HTTP 触发的 TCP 转发） ==============

    async def create_pending_tcp_request(self, conn_id: str, domain: str = "") -> asyncio.Future:
        """创建待响应的 TCP 请求"""
        future = asyncio.get_event_loop().create_future()
        self._pending_tcp_requests[conn_id] = PendingTcpRequest(
            conn_id=conn_id,
            future=future,
            domain=domain,
        )
        return future

    def get_tcp_owner_domain(self, conn_id: str) -> str | None:
        """查询 conn_id 的归属隧道（不存在返回 None；跨隧道串扰校验用）

        优先查 HTTP 触发的 pending TCP 转发，其次查服务端监听的真实 TCP 连接。
        """
        pending = self._pending_tcp_requests.get(conn_id)
        if pending:
            return pending.domain or None
        tcp_conn = self._tcp_connections.get(conn_id)
        if tcp_conn:
            return tcp_conn.domain or None
        return None

    async def handle_tcp_response_data(self, conn_id: str, data: bytes) -> bool:
        """累积客户端返回的 TCP 响应数据

        累积超过 tcp_forward_max_buffer_bytes（0 = 不限）时立即以
        "response too large" 错误完成该请求，防止 /forward 拉大文件 OOM。
        """
        pending = self._pending_tcp_requests.get(conn_id)
        if not pending:
            return False
        pending.chunks.append(data)
        pending.total_bytes += len(data)

        limit = self.tcp_forward_max_buffer_bytes
        if limit > 0 and pending.total_bytes > limit:
            logger.warning(
                f"TCP 响应超过缓冲上限 ({limit} bytes)，终止转发: "
                f"conn_id={conn_id}, domain={pending.domain}"
            )
            # 从注册表移除并以错误完成 future（forward 返回 502）
            self._pending_tcp_requests.pop(conn_id, None)
            if not pending.future.done():
                pending.future.set_result({"error": "response too large", "data": b""})
        return True

    async def complete_tcp_request(self, conn_id: str, error: str | None = None) -> bool:
        """完成 TCP 请求（客户端关闭连接时调用）"""
        pending = self._pending_tcp_requests.pop(conn_id, None)
        if pending and not pending.future.done():
            if error:
                pending.future.set_result({"error": error, "data": b""})
            else:
                # 合并所有数据块
                full_data = b"".join(pending.chunks)
                pending.future.set_result({"error": None, "data": full_data})
            return True
        return False

    async def cleanup_tcp_request(self, conn_id: str) -> None:
        """清理 TCP 请求"""
        pending = self._pending_tcp_requests.pop(conn_id, None)
        if pending and not pending.future.done():
            pending.future.cancel()

    # ============== UDP 会话表（协议 v2 udp） ==============

    def add_udp_session(self, key: tuple, session: UdpSessionState) -> None:
        """登记 UDP 会话（会话键 = (监听端口, 外部 addr)；同键覆盖视为重建）"""
        self._udp_sessions[key] = session

    def get_udp_session_by_addr(self, key: tuple) -> UdpSessionState | None:
        """按会话键（监听端口 + 外部 addr）查会话"""
        return self._udp_sessions.get(key)

    def get_udp_session(self, session_id: str) -> UdpSessionState | None:
        """按 session_id 查会话（O(n) 扫描；会话规模受 udp_max_sessions 约束）"""
        for session in self._udp_sessions.values():
            if session.session_id == session_id:
                return session
        return None

    def remove_udp_session(self, key: tuple) -> UdpSessionState | None:
        """按会话键移除会话，返回被移除的会话（不存在返回 None）"""
        return self._udp_sessions.pop(key, None)

    def remove_udp_session_by_id(self, session_id: str) -> UdpSessionState | None:
        """按 session_id 移除会话（客户端主动 udp_close 用）"""
        key = next(
            (k for k, s in self._udp_sessions.items() if s.session_id == session_id),
            None,
        )
        return self._udp_sessions.pop(key, None) if key is not None else None

    def count_udp_sessions(self, domain: str) -> int:
        """统计指定隧道的活跃 UDP 会话数（udp_max_sessions 限额 / metrics 用）"""
        return sum(1 for s in self._udp_sessions.values() if s.domain == domain)

    def list_udp_session_domains(self) -> list[str]:
        """列出当前有活跃 UDP 会话的隧道域名（去重排序）"""
        return sorted({s.domain for s in self._udp_sessions.values() if s.domain})

    def iter_udp_sessions(self) -> list[tuple[tuple, UdpSessionState]]:
        """会话表快照（sweeper 遍历用，避免遍历时变更）"""
        return list(self._udp_sessions.items())

    def cleanup_udp_sessions_for_domain(self, domain: str) -> int:
        """清理指定隧道的全部 UDP 会话（隧道断连时调用；同步无 IO，可在锁内调用）"""
        if not domain:
            return 0
        stale = [k for k, s in self._udp_sessions.items() if s.domain == domain]
        for key in stale:
            self._udp_sessions.pop(key, None)
        return len(stale)

    def clear_udp_sessions(self) -> int:
        """清空全部 UDP 会话（服务端停机时调用），返回清理数"""
        n = len(self._udp_sessions)
        self._udp_sessions.clear()
        return n


# ============== 隧道服务器 ==============


class _UdpDatagramProtocol(asyncio.DatagramProtocol):
    """UDP 监听器协议（协议 v2 udp）

    datagram_received 是事件循环内的同步回调，不能 await——这里只做
    有界入队（队满丢包，UDP 有损语义），由 TunnelServer 为每监听器启动的
    pump 任务顺序消费：入包顺序严格保持，udp_open 严格先于首个数据帧。
    """

    def __init__(
        self, server: "TunnelServer", port: int, domain: str, queue: asyncio.Queue
    ):
        self.server = server
        self.port = port
        self.domain = domain
        self.queue = queue
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self.queue.put_nowait((data, addr))
        except asyncio.QueueFull:
            logger.warning(
                f"UDP 收包队列已满，丢包: port={self.port}, addr={addr}, size={len(data)}"
            )

    def error_received(self, exc: Exception | None) -> None:
        # 收包路径的 ICMP 错误等（UDP 监听 socket 本身通常不因此关闭）
        logger.debug(f"UDP 监听器错误（忽略）: port={self.port}, {exc}")


class TunnelServer:
    """
    隧道服务器

    提供：
    1. WebSocket 端点用于客户端连接
    2. HTTP API 用于管理隧道
    3. 转发方法用于发送请求到客户端
    """

    def __init__(self, config: TunnelServerConfig | None = None):
        self.config = config or TunnelServerConfig()
        self.db: DatabaseManager | None = None
        self.manager = TunnelManager(
            tcp_forward_max_buffer_bytes=self.config.tcp_forward_max_buffer_bytes,
            stream_queue_maxsize=self.config.stream_queue_maxsize,
        )
        self.router = APIRouter(tags=["Tunnel"])
        self._tcp_servers: list[asyncio.Server] = []
        # 监听端口 -> 绑定的隧道域名（多监听器路由）
        self._listener_domains: dict[int, str] = {}
        # UDP 监听器（协议 v2 udp）：端口 → transport / 绑定的隧道域名
        self._udp_transports: dict[int, asyncio.DatagramTransport] = {}
        self._udp_listener_domains: dict[int, str] = {}
        # 每个 UDP 监听器的收包队列（datagram_received 同步回调入队，
        # pump 任务顺序消费——严格保持入包顺序，udp_open 严格先于首个数据帧）
        self._udp_queues: dict[int, asyncio.Queue] = {}
        self._udp_pump_tasks: list[asyncio.Task] = []
        self._udp_sweeper_task: asyncio.Task | None = None
        # 每隧道流量统计（内存态计数，周期性落库）：domain -> {"bytes_in": n, "bytes_out": n}
        # bytes_in = 外部 → 内网服务；bytes_out = 内网服务 → 外部
        self._tunnel_bytes: dict[str, dict[str, int]] = {}
        # 已落库快照（initialize 时从 DB 行 seed），flush 只写「当前值 - 快照」的增量
        self._flushed_bytes: dict[str, dict[str, int]] = {}
        # 每隧道请求计数（内存态增量，与流量统计同一周期落库）：
        # 转发热路径只累加内存，不再每请求一次 DB 事务
        self._request_counters: dict[str, int] = {}
        self._bytes_flush_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()

        # 请求日志后台写队列：转发面只入队，落库由专职 worker 串行消费；
        # 队满丢弃并计数（tunely_request_logs_dropped_total 可观测）
        self._log_queue: asyncio.Queue | None = None
        self._log_worker_task: asyncio.Task | None = None
        self._request_logs_dropped = 0
        self._log_retention_task: asyncio.Task | None = None

        # 注册路由
        self._register_routes()

    async def initialize(self) -> None:
        """初始化服务器"""
        self.db = DatabaseManager(self.config.database_url)
        await self.db.initialize()
        logger.info("TunnelServer 初始化完成")

        # 从 DB 行恢复流量统计初值（live 值跨重启连续）
        await self._seed_tunnel_bytes()

        # 如果配置了 TCP 监听端口，启动 TCP 监听
        await self._start_tcp_listeners()

        # 如果配置了 UDP 监听端口，启动 UDP 监听（协议 v2 udp）
        await self._start_udp_listeners()

        # 启动流量统计周期落库任务
        self._start_bytes_flush_task()

        # 启动请求日志后台写 worker 与保留清理任务
        if self.config.request_log_enabled:
            self._log_queue = asyncio.Queue(maxsize=_LOG_QUEUE_MAXSIZE)
            self._log_worker_task = asyncio.create_task(self._request_log_worker())
        if self.config.request_log_retention_days > 0:
            self._log_retention_task = asyncio.create_task(
                self._request_log_retention_loop()
            )

    async def close(self) -> None:
        """关闭服务器"""
        # 停掉请求日志后台 worker：先发哨兵等它清空队列（有限时），
        # 超时则放弃积压日志直接取消
        if self._log_worker_task:
            if self._log_queue is not None:
                try:
                    self._log_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            try:
                await asyncio.wait_for(self._log_worker_task, timeout=5.0)
            except asyncio.CancelledError:
                raise  # close 自身被取消，向上传播
            except Exception:
                # 超时（wait_for 已取消 worker）或其他异常：兜底再 cancel 一次
                self._log_worker_task.cancel()
            self._log_worker_task = None
        if self._log_retention_task:
            self._log_retention_task.cancel()
            try:
                await self._log_retention_task
            except asyncio.CancelledError:
                pass
            self._log_retention_task = None
        # 停掉流量统计周期任务，并尽力把最后的增量落库
        if self._bytes_flush_task:
            self._bytes_flush_task.cancel()
            try:
                await self._bytes_flush_task
            except asyncio.CancelledError:
                pass
            self._bytes_flush_task = None
        try:
            await self._flush_tunnel_bytes()
            await self._flush_request_counters()
        except Exception as e:
            logger.warning(f"停机前流量统计落库失败（忽略）: {e}")
        # 先清理活跃的外部 TCP 连接（否则 wait_closed 会等它们自然结束）
        await self.manager.close_all_tcp_connections()
        # 关闭全部 TCP 监听器
        for tcp_server in self._tcp_servers:
            tcp_server.close()
        for tcp_server in self._tcp_servers:
            await tcp_server.wait_closed()
        if self._tcp_servers:
            logger.info(f"TCP 监听器已关闭（{len(self._tcp_servers)} 个）")
        self._tcp_servers = []
        self._listener_domains.clear()
        # 停 UDP sweeper、关 UDP 监听器与 pump、清会话表（协议 v2 udp）
        if self._udp_sweeper_task:
            self._udp_sweeper_task.cancel()
            try:
                await self._udp_sweeper_task
            except asyncio.CancelledError:
                pass
            self._udp_sweeper_task = None
        for transport in self._udp_transports.values():
            try:
                transport.close()
            except Exception:
                pass
        self._udp_transports.clear()
        self._udp_listener_domains.clear()
        for pump_task in self._udp_pump_tasks:
            pump_task.cancel()
        if self._udp_pump_tasks:
            await asyncio.gather(*self._udp_pump_tasks, return_exceptions=True)
        self._udp_pump_tasks = []
        self._udp_queues.clear()
        closed_sessions = self.manager.clear_udp_sessions()
        if closed_sessions:
            logger.info(f"UDP 会话已清理（{closed_sessions} 个）")
        if self.db:
            await self.db.close()
        logger.info("TunnelServer 已关闭")

    def _register_routes(self) -> None:
        """注册路由"""

        @self.router.websocket(self.config.ws_path)
        async def websocket_endpoint(websocket: WebSocket):
            await self._handle_websocket(websocket)

        @self.router.post("/api/tunnels", response_model=CreateTunnelResponse)
        async def create_tunnel(
            request: CreateTunnelRequest,
            http_request: Request,
            x_api_key: str | None = Header(None, alias="x-api-key"),
            authorization: str | None = Header(None),
        ):
            return await self._create_tunnel(
                request, x_api_key, authorization, source_ip=_client_ip(http_request)
            )

        @self.router.get("/api/tunnels", response_model=list[TunnelInfo])
        async def list_tunnels(
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._list_tunnels(x_api_key)

        # 注意：check-availability 必须在 {domain} 之前注册，避免被当作 domain 匹配
        @self.router.get(
            "/api/tunnels/check-availability", response_model=CheckAvailabilityResponse
        )
        async def check_availability(name: str):
            return await self._check_availability(name)

        @self.router.get("/api/tunnels/{domain}", response_model=TunnelInfo)
        async def get_tunnel(
            domain: str,
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._get_tunnel(domain, x_api_key)

        @self.router.put("/api/tunnels/{domain}", response_model=TunnelInfo)
        async def update_tunnel(
            domain: str,
            request: UpdateTunnelRequest,
            http_request: Request,
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._update_tunnel(
                domain, request, x_api_key, source_ip=_client_ip(http_request)
            )

        @self.router.delete("/api/tunnels/{domain}")
        async def delete_tunnel(
            domain: str,
            http_request: Request,
            x_api_key: str | None = Header(None, alias="x-api-key"),
            x_tunnel_token: str | None = Header(None, alias="x-tunnel-token"),
        ):
            return await self._delete_tunnel(
                domain, x_api_key, x_tunnel_token, source_ip=_client_ip(http_request)
            )

        @self.router.post(
            "/api/tunnels/{domain}/regenerate-token", response_model=RegenerateTokenResponse
        )
        async def regenerate_token(
            domain: str,
            http_request: Request,
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._regenerate_token(
                domain, x_api_key, source_ip=_client_ip(http_request)
            )

        @self.router.post("/api/tunnels/{domain}/forward", response_model=ForwardResponse)
        async def forward_request(
            domain: str,
            request: ForwardRequest,
        ):
            # path 安全校验（@-SSRF）：非法 path 直接 400，不触达隧道客户端
            if not is_valid_forward_path(request.path):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid path: must start with '/'",
                )
            return await self.forward(
                domain=domain,
                method=request.method,
                path=request.path,
                headers=request.headers,
                body=request.body,
                timeout=request.timeout,
            )

        @self.router.get("/api/tunnels/{domain}/logs")
        async def get_tunnel_logs(
            domain: str,
            limit: int = Query(100, ge=1, le=1000),
            offset: int = Query(0, ge=0),
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            """获取隧道请求历史日志"""
            return await self._get_tunnel_logs(domain, limit, offset, x_api_key)

        @self.router.get("/api/audit")
        async def get_audit_logs(
            limit: int = Query(50, ge=1, le=1000),
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            """获取管理面审计日志（倒序）"""
            return await self._get_audit_logs(limit, x_api_key)

        @self.router.get(
            "/metrics",
            include_in_schema=False,
        )
        async def get_metrics():
            """
            Prometheus 指标端点（文本格式，无鉴权）。

            注意：勿暴露公网，与 /api 同理。
            """
            from fastapi.responses import Response

            registered = 0
            if self.db:
                try:
                    async with self.db.session() as session:
                        repo = TunnelRepository(session)
                        registered = await repo.count_tunnels()
                except Exception as e:
                    logger.warning(f"读取隧道总数失败（metrics 降级为 0）: {e}")
            text = self._render_prometheus_metrics(registered=registered)
            return Response(content=text, media_type="text/plain; version=0.0.4")

        @self.router.get("/api/info")
        async def get_server_info():
            """获取服务信息和域名配置规则"""
            ws_url = self.config.ws_url or f"wss://{self.config.domain}{self.config.ws_path}"

            server_version = self._server_version()
            result: dict[str, Any] = {
                "name": "Tunely Server",
                "version": server_version,
                "domain": {
                    "pattern": f"{{subdomain}}.{self.config.domain}",
                    "customizable": "subdomain",
                    "suffix": f".{self.config.domain}",
                },
                "websocket": {
                    "url": ws_url,
                },
                "protocols": ["https", "http"],
                "auth": {
                    "required": self.config.jwt_secret is not None,
                    "type": "bearer" if self.config.jwt_secret else None,
                },
            }
            
            if self.config.instruction:
                result["instruction"] = self.config.instruction
            
            return result

    @staticmethod
    def _server_version() -> str:
        """真实服务端版本（AuthOk.server_version 与 /api/info 同源，0.7.2 起不再谎报 0.1.0）

        优先取执行代码自身的 tunely.__version__（install 元数据可能过期谎报），
        取不到再退 importlib.metadata。
        """
        try:
            import tunely

            return tunely.__version__
        except Exception:
            try:
                from importlib.metadata import version as _pkg_version

                return _pkg_version("tunely")
            except Exception:
                return "unknown"

    def _check_admin_api_key(self, api_key: str | None) -> None:
        """检查管理 API 密钥（常数时间比较，防时序侧信道）"""
        if self.config.admin_api_key:
            provided = api_key or ""
            if not hmac.compare_digest(provided, self.config.admin_api_key):
                raise HTTPException(status_code=401, detail="Invalid API key")

    # 请求日志中必须脱敏的 header（大小写不敏感）
    _SENSITIVE_LOG_HEADERS = {"authorization", "cookie", "set-cookie"}

    @classmethod
    def _redact_headers_for_log(cls, headers: dict[str, str] | None) -> dict[str, str] | None:
        """请求日志存储前的 header 脱敏：敏感凭证值替换为 "[REDACTED]"

        键名大小写不敏感；其余原样保留（返回副本，不改写调用方数据）。
        """
        if headers is None:
            return None
        return {
            key: ("[REDACTED]" if key.lower() in cls._SENSITIVE_LOG_HEADERS else value)
            for key, value in headers.items()
        }

    async def _notify_connected(self, domain: str) -> None:
        """向 as-dispatch 发送客户端连接 webhook（fire-and-forget）"""
        if not self.config.dispatch_webhook_url:
            return
        url = f"{self.config.dispatch_webhook_url.rstrip('/')}/api/tunnel/connected"
        payload = {"domain": domain, "event": "connected"}
        body_bytes = json.dumps(payload, separators=(",", ":")).encode()
        headers: dict[str, str] = {}
        if self.config.dispatch_webhook_secret:
            digest = hmac.new(
                self.config.dispatch_webhook_secret.encode(),
                body_bytes,
                hashlib.sha256,
            ).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={digest}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, content=body_bytes, headers={**headers, "Content-Type": "application/json"})
                logger.info(f"连接 webhook 已发送: domain={domain}, status={resp.status_code}")
        except Exception as e:
            logger.warning(f"连接 webhook 发送失败（忽略）: domain={domain}, error={e}")

    # 域名格式：字母数字开头，可包含中划线，长度 1-63
    DOMAIN_PATTERN = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9]{0,62}$")

    async def _check_availability(self, name: str) -> CheckAvailabilityResponse:
        """检查隧道名称是否可用"""
        # 验证格式
        if not self.DOMAIN_PATTERN.match(name):
            return CheckAvailabilityResponse(
                available=False,
                name=name,
                reason="Invalid domain format. Use letters, numbers, and hyphens only (1-63 chars, start with letter/number)",
            )

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)
            existing = await repo.get_by_domain(name)

            if existing:
                return CheckAvailabilityResponse(
                    available=False,
                    name=name,
                    reason="Domain already exists",
                )

            return CheckAvailabilityResponse(
                available=True,
                name=name,
                reason=None,
            )

    def _reject_cross_tunnel_message(
        self,
        kind: str,
        msg_key: str,
        owner_domain: str | None,
        conn_domain: str | None,
    ) -> bool:
        """
        跨隧道消息归属校验

        owner_domain 为 None 表示没有对应的 pending 记录（交由原处理路径自然
        no-op）；存在且与当前连接 domain 不一致时，判定为疑似跨隧道串扰，
        告警并丢弃消息。返回 True 表示消息应被丢弃。
        """
        if owner_domain is None:
            return False
        if conn_domain is None or owner_domain == conn_domain:
            return False
        logger.warning(
            f"疑似跨隧道串扰: 连接 domain={conn_domain} 发送了属于 "
            f"domain={owner_domain} 的 {kind} 消息 (key={msg_key})，已丢弃"
        )
        return True

    def _negotiate_capabilities(self, client_caps: list) -> list[str]:
        """协议 v2 能力协商：服务端注册表 ∩ 客户端声明 − kill 开关禁用集。

        铁律（docs/PROTOCOL.md「能力协商」）：字段缺失 = 空集合；只回交集——
        客户端声明了但服务端未注册（未实现）的能力不会启用。
        禁用集来自 config.disable_capabilities（逗号分隔，strip 后剔除）。
        防御式：非列表输入按空集合处理，畸形声明不影响认证。
        """
        disabled = {
            name.strip()
            for name in (self.config.disable_capabilities or "").split(",")
            if name.strip()
        }
        if not isinstance(client_caps, (list, tuple, set, frozenset)):
            return []
        client_set = {c for c in client_caps if isinstance(c, str)}
        return sorted((set(SERVER_CAPABILITIES) & client_set) - disabled)

    async def _handle_websocket(self, websocket: WebSocket) -> None:
        """处理 WebSocket 连接"""
        await websocket.accept()

        token: str | None = None
        tunnel_domain: str | None = None
        success: bool = False

        try:
            # 等待认证消息
            raw_message = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=30.0,
            )
            data = json.loads(raw_message)
            message = parse_message(data)

            if not isinstance(message, AuthMessage):
                await websocket.send_text(
                    AuthErrorMessage(error="Expected auth message").model_dump_json()
                )
                # 认证失败限速：延迟后关闭，提高暴力尝试成本
                await asyncio.sleep(_AUTH_FAILURE_DELAY)
                await websocket.close(code=1008)
                return

            token = message.token

            # 验证令牌
            if not self.db:
                await websocket.send_text(
                    AuthErrorMessage(error="Database not initialized").model_dump_json()
                )
                await websocket.close(code=1011)
                return

            async with self.db.session() as session:
                repo = TunnelRepository(session)
                tunnel = await repo.get_by_token(token)

                if not tunnel:
                    await websocket.send_text(
                        AuthErrorMessage(error="Invalid token").model_dump_json()
                    )
                    # 认证失败限速：延迟后关闭，防止无限速暴力尝试 token
                    await asyncio.sleep(_AUTH_FAILURE_DELAY)
                    await websocket.close(code=1008)
                    return

                if not tunnel.enabled:
                    await websocket.send_text(
                        AuthErrorMessage(error="Tunnel is disabled").model_dump_json()
                    )
                    await websocket.close(code=1008)
                    return

                tunnel_domain = tunnel.domain
                tunnel_mode = tunnel.mode

                # 更新最后连接时间
                await repo.update_last_connected(token)

                # 尝试注册连接（记录注册前是否已有连接，用于 takeover 审计）
                force = getattr(message, 'force', False)
                had_existing = self.manager.get_connection_by_token(token) is not None
                # 协议 v2 能力协商：客户端缺失/空声明按空集合，AuthOk 只回交集；
                # 协商结果同时存入 ActiveConnection（后续按连接门控，本任务只存不用）
                negotiated_caps = self._negotiate_capabilities(
                    getattr(message, "capabilities", None) or []
                )
                success, error = await self.manager.register(
                    websocket=websocket,
                    tunnel_id=tunnel.id,
                    domain=tunnel.domain,
                    token=token,
                    force=force,
                    mode=tunnel_mode,
                    client_version=getattr(message, "client_version", None) or "unknown",
                    capabilities=frozenset(negotiated_caps),
                )

                if not success:
                    await websocket.send_text(
                        AuthErrorMessage(
                            error=error or "Connection rejected",
                            code="connection_exists",
                        ).model_dump_json()
                    )
                    await websocket.close(code=1008)
                    return

                # 审计：force 抢占成功
                if force and had_existing:
                    await self._record_audit(
                        "takeover",
                        domain=tunnel.domain,
                        detail="forced takeover of existing connection",
                        source_ip=_ws_client_ip(websocket),
                    )

                # 发送认证成功
                await websocket.send_text(
                    AuthOkMessage(
                        domain=tunnel.domain,
                        tunnel_id=str(tunnel.id),
                        server_version=self._server_version(),
                        capabilities=negotiated_caps,
                    ).model_dump_json()
                )

                # 发送连接 webhook（fire-and-forget）
                task = asyncio.create_task(self._notify_connected(tunnel.domain))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            # 处理消息循环
            while True:
                # 协议 v2 binary_frames（T2）：receive_text() 改 receive() 分派——
                # text 消息（key="text"）走原 JSON 全流程；binary 消息（key="bytes"）
                # 仅协商连接解帧路由。对 text 消息的取值与断连语义与 receive_text()
                # 逐字一致（_raise_on_disconnect），未协商路径行为不变。
                ws_msg = await websocket.receive()
                if ws_msg["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(ws_msg["code"], ws_msg.get("reason"))
                if ws_msg.get("bytes") is not None:
                    await self._handle_ws_binary_message(
                        token, tunnel_domain, ws_msg["bytes"]
                    )
                    continue
                raw_message = ws_msg["text"]
                # F10：畸形消息（非法 JSON / 未知类型 / 字段形状不对）只丢弃
                # 该条并告警，不得让整条隧道断连注销（断连会让全部在途请求悬挂）。
                # 0.7.3 起热路径消息走 parse_message_fast 轻校验（ValueError 同样在此捕获）
                try:
                    message = parse_message_fast(raw_message)
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"丢弃畸形 WS 消息: domain={tunnel_domain}, error={e}, "
                        f"raw={raw_message[:200]!r}"
                    )
                    continue

                if isinstance(message, PingMessage):
                    # 协议级 keepalive：客户端周期性发 Ping，必须回 Pong，
                    # 否则客户端看门狗会误判连接死亡而重连（0.6.1 回归修复）
                    await self.manager.update_heartbeat(token)
                    await websocket.send_text(PongMessage().model_dump_json())
                elif isinstance(message, PongMessage):
                    await self.manager.update_heartbeat(token)
                elif isinstance(message, TunnelResponse):
                    await self._handle_client_tunnel_response(message, tunnel_domain)
                # 流式消息处理（SSE 支持）
                elif isinstance(message, StreamStartMessage):
                    if not self._reject_cross_tunnel_message(
                        "stream_start",
                        message.id,
                        self.manager.get_pending_stream_domain(message.id),
                        tunnel_domain,
                    ):
                        await self.manager.handle_stream_start(message)
                elif isinstance(message, StreamChunkMessage):
                    if not self._reject_cross_tunnel_message(
                        "stream_chunk",
                        message.id,
                        self.manager.get_pending_stream_domain(message.id),
                        tunnel_domain,
                    ):
                        await self.manager.handle_stream_chunk(message)
                elif isinstance(message, StreamEndMessage):
                    if not self._reject_cross_tunnel_message(
                        "stream_end",
                        message.id,
                        self.manager.get_pending_stream_domain(message.id),
                        tunnel_domain,
                    ):
                        await self.manager.handle_stream_end(message)
                # TCP 消息处理
                elif isinstance(message, TcpDataMessage):
                    if not self._reject_cross_tunnel_message(
                        "tcp_data",
                        message.conn_id,
                        self.manager.get_tcp_owner_domain(message.conn_id),
                        tunnel_domain,
                    ):
                        await self._handle_tcp_data_from_client(message)
                elif isinstance(message, TcpCloseMessage):
                    if not self._reject_cross_tunnel_message(
                        "tcp_close",
                        message.conn_id,
                        self.manager.get_tcp_owner_domain(message.conn_id),
                        tunnel_domain,
                    ):
                        await self._handle_tcp_close_from_client(message)
                # UDP 消息处理（协议 v2 udp）：数据面走 0x03 binary 帧（见
                # _handle_ws_binary_message 分派），控制面仅 udp_close（客户端主动关闭）
                elif isinstance(message, UdpCloseMessage):
                    await self._handle_udp_close_from_client(message, tunnel_domain)
                else:
                    logger.warning(f"未知消息类型: {type(message)}")

        except WebSocketDisconnect:
            logger.info(f"WebSocket 断开: domain={tunnel_domain}")
        except asyncio.TimeoutError:
            logger.warning("认证超时")
            try:
                await websocket.close(code=1008)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"WebSocket 错误: {e}", exc_info=True)
        finally:
            if token and success:
                # 仅当注册表仍指向当前连接时才注销——force 抢占后
                # 注册表已是新连接，旧连接的退出不得误删（0.6.1 回归修复）
                await self.manager.unregister(token, websocket=websocket)

    async def _handle_ws_binary_message(
        self, token: str | None, tunnel_domain: str | None, payload: bytes
    ) -> None:
        """处理 WS binary 消息（协议 v2 binary_frames，能力门控）

        - 未协商该能力的连接收到 binary → warning + 丢弃（F10 语义，不断连）
        - 帧类型分派（协议 v2 T4）：0x03 = udp_data 走 UDP 会话路径（另需
          该连接协商了 udp）；0x01 及其余按 tcp_data 解帧
        - 畸形帧（版本/类型/长度不对）→ warning + 丢弃
        - 解出 conn_id + payload 后与 TcpDataMessage 的 JSON 路径汇合：
          同样的跨隧道归属校验 + _route_tcp_payload 落地
        """
        conn = self.manager.get_connection_by_token(token) if token else None
        if conn is None or "binary_frames" not in conn.capabilities:
            logger.warning("未协商 binary_frames，收到 WS 二进制消息已丢弃")
            return
        if len(payload) >= 2 and payload[1] == FRAME_TYPE_UDP_DATA:
            await self._handle_udp_data_frame(conn, tunnel_domain, payload)
            return
        try:
            conn_id, data = decode_tcp_data_frame(payload)
        except ValueError as e:
            logger.warning(f"丢弃畸形 binary 帧: {e}")
            return
        if self._reject_cross_tunnel_message(
            "tcp_data",
            conn_id,
            self.manager.get_tcp_owner_domain(conn_id),
            tunnel_domain,
        ):
            return
        await self._route_tcp_payload(conn_id, data)

    async def _handle_client_tunnel_response(
        self, message: TunnelResponse, tunnel_domain: str | None
    ) -> None:
        """处理客户端回传的 TunnelResponse（缓冲与流式两种归属）

        - 归属校验覆盖两种 pending（缓冲 _pending_requests / 流式
          _pending_stream_requests，防跨隧道串扰）；
        - 协议 v2 chunked_http（T3）补桥：id 属于 forward_stream 的流式
          pending（非 SSE 目标，客户端只会回缓冲形态 TunnelResponse）时，
          合成 StreamStart + StreamChunk + StreamEnd 投入流队列——修复
          「流式消费侧干等到超时」的既有缺口；
        - 否则走原缓冲完成路径（/forward、/t/ 缓冲分支零行为变化）。
        """
        owner_domain = self.manager.get_pending_request_domain(
            message.id
        ) or self.manager.get_pending_stream_domain(message.id)
        if self._reject_cross_tunnel_message(
            "tunnel_response", message.id, owner_domain, tunnel_domain
        ):
            return
        bridged = await self.manager.bridge_response_to_stream(message)
        if not bridged:
            await self.manager.complete_request(message.id, message)

    def _verify_jwt_token(self, authorization: str | None) -> dict | None:
        """验证 JWT Bearer token，返回 payload 或 None"""
        if not self.config.jwt_secret:
            return None

        if not authorization:
            raise HTTPException(
                status_code=401,
                detail="Authorization header required (Bearer <token>)"
            )

        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="Invalid authorization format. Use: Bearer <token>"
            )

        token = parts[1]
        try:
            payload = pyjwt.decode(
                token,
                self.config.jwt_secret,
                algorithms=["HS256"],
            )
            return payload
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token has expired")
        except pyjwt.InvalidTokenError as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    async def _record_audit(
        self,
        action: str,
        domain: str | None = None,
        detail: str | None = None,
        source_ip: str | None = None,
    ) -> None:
        """记录管理面审计日志（尽力而为：失败只告警，不影响主流程）"""
        if not self.db:
            return
        try:
            async with self.db.session() as session:
                repo = AdminAuditLogRepository(session)
                await repo.create(
                    action=action, domain=domain, detail=detail, source_ip=source_ip
                )
        except Exception as e:
            logger.warning(f"审计日志写入失败（忽略）: action={action}, domain={domain}, error={e}")

    async def _get_audit_logs(self, limit: int, api_key: str | None) -> list[dict]:
        """查询管理面审计日志（倒序）"""
        self._check_admin_api_key(api_key)

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = AdminAuditLogRepository(session)
            logs = await repo.list_recent(limit=limit)
            return [log.to_dict() for log in logs]

    async def _create_tunnel(
        self,
        request: CreateTunnelRequest,
        api_key: str | None,
        authorization: str | None = None,
        source_ip: str | None = None,
    ) -> CreateTunnelResponse:
        """创建隧道 - 支持 JWT 认证（公网模式）或无认证（内网模式）"""
        # 配置了 admin api-key 时，创建与查询/删除同样强制鉴权（0.5.0 起；
        # 未配置 key 的部署保持内网模式语义不变）
        self._check_admin_api_key(api_key)
        jwt_payload = self._verify_jwt_token(authorization)

        # 域名格式校验（与 check-availability 同一规则）：
        # 非法 domain 不入库，统一 400
        if not self.DOMAIN_PATTERN.match(request.domain or ""):
            raise HTTPException(
                status_code=400,
                detail="Invalid domain format. Use letters, numbers, and hyphens only (1-63 chars, start with letter/number)",
            )

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)

            # 检查域名是否已存在
            existing = await repo.get_by_domain(request.domain)
            if existing:
                raise HTTPException(status_code=409, detail="Domain already exists")

            tunnel = await repo.create(
                domain=request.domain,
                name=request.name,
                description=request.description,
                mode=request.mode,
            )

            await session.commit()
            await session.refresh(tunnel)

            await self._record_audit(
                "create",
                domain=tunnel.domain,
                detail=f"mode={request.mode}",
                source_ip=source_ip,
            )

            return CreateTunnelResponse(
                domain=tunnel.domain,
                token=tunnel.token,
                name=tunnel.name,
                mode=tunnel.mode,
            )

    async def _list_tunnels(self, api_key: str | None) -> list[TunnelInfo]:
        """列出所有隧道"""
        self._check_admin_api_key(api_key)

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)
            # 移除 limit 限制，返回所有隧道（原默认 limit=100）
            tunnels = await repo.list_all(limit=999999)

            return [
                TunnelInfo(
                    domain=t.domain,
                    name=t.name,
                    description=t.description,
                    mode=t.mode,
                    enabled=t.enabled,
                    **self._tunnel_byte_stats(t.domain),
                    connected=self.manager.is_connected(t.domain),
                    created_at=t.created_at.isoformat() if t.created_at else None,
                    last_connected_at=(
                        t.last_connected_at.isoformat() if t.last_connected_at else None
                    ),
                    total_requests=self._live_request_count(t.domain, t.total_requests),
                    client_version=self.manager.get_client_version(t.domain),
                )
                for t in tunnels
            ]

    async def _get_tunnel(self, domain: str, api_key: str | None) -> TunnelInfo:
        """获取隧道详情"""
        self._check_admin_api_key(api_key)

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.get_by_domain(domain)

            if not tunnel:
                raise HTTPException(status_code=404, detail="Tunnel not found")

            return TunnelInfo(
                domain=tunnel.domain,
                name=tunnel.name,
                description=tunnel.description,
                mode=tunnel.mode,
                enabled=tunnel.enabled,
                **self._tunnel_byte_stats(tunnel.domain),
                connected=self.manager.is_connected(tunnel.domain),
                created_at=tunnel.created_at.isoformat() if tunnel.created_at else None,
                last_connected_at=(
                    tunnel.last_connected_at.isoformat()
                    if tunnel.last_connected_at
                    else None
                ),
                total_requests=self._live_request_count(
                    tunnel.domain, tunnel.total_requests
                ),
                client_version=self.manager.get_client_version(tunnel.domain),
            )

    async def _update_tunnel(
        self,
        domain: str,
        request: UpdateTunnelRequest,
        api_key: str | None,
        source_ip: str | None = None,
    ) -> TunnelInfo:
        """更新隧道"""
        self._check_admin_api_key(api_key)

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.get_by_domain(domain)

            if not tunnel:
                raise HTTPException(status_code=404, detail="Tunnel not found")

            # 更新字段
            update_values = {}
            if request.name is not None:
                update_values['name'] = request.name
            if request.description is not None:
                update_values['description'] = request.description
            if request.enabled is not None:
                update_values['enabled'] = request.enabled
            if request.mode is not None:
                update_values['mode'] = request.mode
            if update_values:
                update_values['updated_at'] = datetime.now(timezone.utc)

            if update_values:
                from sqlalchemy import update as sql_update
                await session.execute(
                    sql_update(Tunnel)
                    .where(Tunnel.domain == domain)
                    .values(**update_values)
                )
                await session.commit()
                await session.refresh(tunnel)

                # 审计：记录变更字段名列表
                changed = sorted(k for k in update_values if k != "updated_at")
                await self._record_audit(
                    "update",
                    domain=domain,
                    detail=f"changed:{','.join(changed)}",
                    source_ip=source_ip,
                )

            return TunnelInfo(
                domain=tunnel.domain,
                name=tunnel.name,
                description=tunnel.description,
                mode=tunnel.mode,
                enabled=tunnel.enabled,
                **self._tunnel_byte_stats(tunnel.domain),
                connected=self.manager.is_connected(tunnel.domain),
                created_at=tunnel.created_at.isoformat() if tunnel.created_at else None,
                last_connected_at=(
                    tunnel.last_connected_at.isoformat()
                    if tunnel.last_connected_at
                    else None
                ),
                total_requests=self._live_request_count(
                    tunnel.domain, tunnel.total_requests
                ),
                client_version=self.manager.get_client_version(tunnel.domain),
            )

    async def _close_tunnel_connection(self, domain: str, reason: str) -> bool:
        """
        关闭隧道的存量 WebSocket 连接（吊销隧道 / 轮换 token 后调用）

        旧连接的 handler 退出时其 finally 会走 unregister 清理；
        0.6.1 的 unregister 身份校验保证 force 抢占场景不误删新连接。
        """
        conn = self.manager.get_connection_by_domain(domain)
        if conn is None:
            return False
        try:
            await conn.websocket.close(code=1000, reason=reason)
        except Exception as e:
            logger.warning(f"关闭隧道存量连接失败（忽略）: domain={domain}, error={e}")
            return False
        logger.info(f"已关闭隧道存量连接: domain={domain}, reason={reason}")
        return True

    async def _regenerate_token(
        self, domain: str, api_key: str | None, source_ip: str | None = None
    ) -> RegenerateTokenResponse:
        """重新生成 Token"""
        self._check_admin_api_key(api_key)

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)
            new_token = await repo.regenerate_token(domain)

            if not new_token:
                raise HTTPException(status_code=404, detail="Tunnel not found")

            await session.commit()

        # 换 token 后立刻断开存量连接（旧 token 不再可用，不应继续服务）
        await self._close_tunnel_connection(domain, reason="token rotated")

        await self._record_audit(
            "regenerate", domain=domain, detail="rotated", source_ip=source_ip
        )

        return RegenerateTokenResponse(domain=domain, token=new_token)

    async def _get_tunnel_logs(
        self, domain: str, limit: int, offset: int, api_key: str | None
    ) -> dict:
        """获取隧道请求历史日志"""
        self._check_admin_api_key(api_key)

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            log_repo = TunnelRequestLogRepository(session)
            logs = await log_repo.get_recent(tunnel_domain=domain, limit=limit, offset=offset)
            total = await log_repo.count(tunnel_domain=domain)

            return {
                "total": total,
                "logs": [log.to_dict() for log in logs],
            }

    async def _delete_tunnel(
        self,
        domain: str,
        api_key: str | None,
        tunnel_token: str | None = None,
        source_ip: str | None = None,
    ) -> dict:
        """删除隧道 - 支持 Admin API Key 或隧道自己的 Token"""
        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)

            # 验证权限:Admin API Key 或隧道自己的 Token
            if tunnel_token:
                # 使用隧道 Token 验证
                tunnel = await repo.get_by_token(tunnel_token)
                if not tunnel or tunnel.domain != domain:
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid tunnel token or domain mismatch"
                    )
                # Token 验证通过,允许删除自己
            else:
                # 使用 Admin API Key 验证
                self._check_admin_api_key(api_key)

            deleted = await repo.delete(domain)

            if not deleted:
                raise HTTPException(status_code=404, detail="Tunnel not found")

        # 删除成功后立刻断开存量连接（隧道已吊销，不应继续服务）
        await self._close_tunnel_connection(domain, reason="tunnel revoked")

        # 审计必须在删除会话提交之后再写：否则 SQLite 下同库第二会话
        # 会被未提交的写锁阻塞，审计静默丢失（0.6.0 实测）
        detail = "revoked"
        if tunnel_token:
            detail += ";via=tunnel-token"
        await self._record_audit(
            "delete",
            domain=domain,
            detail=detail,
            source_ip=source_ip,
        )

        return {"success": True, "domain": domain}

    async def forward(
        self,
        domain: str,
        method: str = "POST",
        path: str = "/",
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout: float = 1800.0,
    ) -> ForwardResponse:
        """
        转发请求到隧道（支持 HTTP 和 TCP 模式）

        Args:
            domain: 目标隧道域名
            method: HTTP 方法（TCP 模式忽略）
            path: 请求路径（TCP 模式忽略）
            headers: 请求头（TCP 模式忽略）
            body: 请求体（TCP 模式为原始二进制数据）
            timeout: 超时时间（秒）

        Returns:
            ForwardResponse
        """
        # path 安全校验（@-SSRF）：非法 path 直接拒绝（HTTP / TCP 模式统一覆盖）
        if not is_valid_forward_path(path):
            return ForwardResponse(
                status=400,
                error="Invalid path: must start with '/'",
            )

        # 转发超时上限 clamp（forward_max_timeout，0 = 不限制）
        if self.config.forward_max_timeout > 0:
            timeout = min(timeout, self.config.forward_max_timeout)

        # 检查连接
        conn = self.manager.get_connection_by_domain(domain)
        if not conn:
            return ForwardResponse(
                status=503,
                error=f"Tunnel not connected: {domain}",
            )

        # 隧道模式：直接用注册时缓存在连接上的 mode（0.7.0 起，
        # 省掉每请求一次 DB 查询；token 轮换/隧道删除后连接仍在时沿用缓存值）
        tunnel_mode = conn.mode if conn.mode in ("http", "tcp") else "http"

        # 根据模式选择转发方式
        if tunnel_mode == "tcp":
            return await self._forward_tcp(domain, body, timeout)
        else:
            return await self._forward_http(domain, method, path, headers, body, timeout)

    async def _write_request_log(
        self,
        domain: str,
        method: str,
        path: str,
        headers: dict[str, str] | None,
        request_body_str: str | None,
        status_code: int | None,
        response_headers: dict[str, str] | None = None,
        response_body: str | None = None,
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        """写入请求日志（独立事务；header 先脱敏）"""
        if not self.db:
            return
        async with self.db.session() as session:
            log_repo = TunnelRequestLogRepository(session)
            await log_repo.create(
                tunnel_domain=domain,
                method=method,
                path=path,
                request_headers=self._redact_headers_for_log(headers),
                request_body=request_body_str,
                status_code=status_code,
                response_headers=self._redact_headers_for_log(response_headers),
                response_body=response_body,
                error=error,
                duration_ms=duration_ms,
            )

    async def _forward_http(
        self,
        domain: str,
        method: str,
        path: str,
        headers: dict[str, str] | None,
        body: Any,
        timeout: float,
    ) -> ForwardResponse:
        """HTTP 模式转发（原 forward 方法的逻辑）"""
        conn = self.manager.get_connection_by_domain(domain)
        if not conn:
            return ForwardResponse(status=503, error=f"Tunnel not connected: {domain}")

        request_id = str(uuid.uuid4())
        # 数据面快速路径：手工 dict + json.dumps，跳过 pydantic 校验/序列化
        # （wire 键集与 TunnelRequest.model_dump 逐字段一致）
        request_body_json = json.dumps(body) if body is not None else None
        try:
            # pending 限额：达到上限直接 503（防慢响应堆积耗尽内存）
            if self.manager.pending_requests_count() >= self.config.max_pending_requests:
                logger.warning(
                    f"pending 请求数已达上限 ({self.config.max_pending_requests})，拒绝转发: domain={domain}"
                )
                return ForwardResponse(
                    status=503,
                    error=f"Too many pending requests (limit={self.config.max_pending_requests})",
                )

            # 创建 Future 等待响应
            future = await self.manager.create_pending_request(request_id, domain=domain)

            # 发送请求
            await conn.websocket.send_text(
                dump_payload(
                    request_payload(
                        request_id, method, path, headers, request_body_json, timeout
                    )
                )
            )

            # 等待响应
            start_time = asyncio.get_event_loop().time()
            response = await asyncio.wait_for(future, timeout=timeout)
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # 请求计数只进内存，随流量统计周期批量落库（转发热路径零 DB 写）
            self._count_tunnel_request(domain)

            # 响应体大小上限（0 = 不限制）：超限拒绝。防大响应把两端
            # 内存与事件循环一起拖垮（TCP 模式有 10MB 缓冲上限，此处对齐）
            cap = self.config.http_max_response_bytes
            if cap > 0 and response.body is not None and len(response.body) > cap:
                error_msg = f"Response too large ({len(response.body)} > limit {cap})"
                self._enqueue_request_log(
                    domain=domain,
                    method=method,
                    path=path,
                    headers=headers,
                    request_body_str=request_body_json[:10000]
                    if request_body_json is not None
                    else None,
                    status_code=response.status,
                    response_headers=response.headers,
                    error=error_msg,
                    duration_ms=duration_ms,
                )
                return ForwardResponse(
                    status=502,
                    error=error_msg,
                    duration_ms=duration_ms,
                )

            # body 只解析一次（返回值用）。日志串沿用既有行为：解析成功
            # 则归一化后截断；超过 _LOG_NORMALIZE_MAX_BODY 的大 body 跳过
            # 归一化（全量 parse+dump 只为取前 1 万字符是纯开销），直接截原始前缀
            parsed_ok = False
            parsed_body: Any = None
            if response.body is not None:
                try:
                    parsed_body = json.loads(response.body)
                    parsed_ok = True
                except (json.JSONDecodeError, ValueError):
                    parsed_body = response.body

            if response.body is None:
                response_body_str = None
            elif not parsed_ok:
                response_body_str = str(parsed_body)[:10000]
            elif len(response.body) <= _LOG_NORMALIZE_MAX_BODY:
                response_body_str = json.dumps(parsed_body)[:10000]
            else:
                response_body_str = response.body[:10000]

            # 记录请求日志（入后台队列，尽力而为，不阻塞转发面）
            self._enqueue_request_log(
                domain=domain,
                method=method,
                path=path,
                headers=headers,
                request_body_str=request_body_json[:10000]
                if request_body_json is not None
                else None,
                status_code=response.status,
                response_headers=response.headers,
                response_body=response_body_str,
                error=response.error,
                duration_ms=duration_ms,
            )

            return ForwardResponse(
                status=response.status,
                headers=response.headers,
                body=parsed_body,
                duration_ms=duration_ms,
                error=response.error,
            )

        except asyncio.TimeoutError:
            error_msg = "Request timeout"
            await self.manager.fail_request(request_id, error_msg)

            # 记录错误日志
            self._enqueue_request_log(
                domain=domain,
                method=method,
                path=path,
                headers=headers,
                request_body_str=request_body_json[:10000]
                if request_body_json is not None
                else None,
                status_code=504,
                error=error_msg,
                duration_ms=int(timeout * 1000),
            )

            return ForwardResponse(
                status=504,
                error=error_msg,
            )
        except Exception as e:
            error_msg = str(e)
            await self.manager.fail_request(request_id, error_msg)

            # 记录错误日志
            self._enqueue_request_log(
                domain=domain,
                method=method,
                path=path,
                headers=headers,
                request_body_str=request_body_json[:10000]
                if request_body_json is not None
                else None,
                status_code=500,
                error=error_msg,
                duration_ms=0,
            )

            return ForwardResponse(
                status=500,
                error=error_msg,
            )

    async def _forward_tcp(
        self,
        domain: str,
        body: Any,
        timeout: float,
    ) -> ForwardResponse:
        """
        TCP 模式转发

        完整的请求-响应闭环:
        1. 创建 PendingTcpRequest + 发送 TcpConnectMessage 给客户端
        2. 客户端建立到目标的 TCP 连接
        3. 发送 TcpDataMessage（请求数据）给客户端
        4. 客户端将数据写入目标 TCP，读取目标响应
        5. 客户端回传 TcpDataMessage（响应数据）→ 累积到 PendingTcpRequest
        6. 客户端发送 TcpCloseMessage → 解析 Future
        7. 返回累积的响应数据
        """
        import base64

        conn = self.manager.get_connection_by_domain(domain)
        if not conn:
            return ForwardResponse(status=503, error=f"Tunnel not connected: {domain}")

        conn_id = str(uuid.uuid4())
        start_time = asyncio.get_event_loop().time()

        try:
            # 1. 创建待响应请求
            future = await self.manager.create_pending_tcp_request(conn_id, domain=domain)

            # 2. 发送 TCP 连接建立消息
            await conn.websocket.send_text(dump_payload(tcp_connect_payload(conn_id)))

            # 3. 发送数据（body is not None 即发送——falsy body 如 0 / "" 不丢）
            if body is not None:
                # 处理不同类型的 body
                if isinstance(body, bytes):
                    data = body
                elif isinstance(body, str):
                    data = body.encode("utf-8")
                else:
                    data = json.dumps(body).encode("utf-8")

                # 编码为 base64 并发送
                await conn.websocket.send_text(
                    dump_payload(
                        tcp_data_payload(
                            conn_id, base64.b64encode(data).decode("ascii"), 0
                        )
                    )
                )

            # 4. 等待客户端响应（TcpDataMessage 累积 + TcpCloseMessage 完成）
            result = await asyncio.wait_for(future, timeout=timeout)

            elapsed = asyncio.get_event_loop().time() - start_time
            duration_ms = int(elapsed * 1000)

            if result.get("error"):
                return ForwardResponse(
                    status=502,
                    error=result["error"],
                    duration_ms=duration_ms,
                )

            # 5. 解析响应数据
            response_data = result.get("data", b"")

            # 尝试将响应解析为 HTTP 响应（如果是 HTTP-over-TCP）
            parsed = self._parse_tcp_response(response_data)

            return ForwardResponse(
                status=parsed.get("status", 200),
                headers=parsed.get("headers", {}),
                body=parsed.get("body", response_data.decode("utf-8", errors="replace") if response_data else ""),
                duration_ms=duration_ms,
            )

        except asyncio.TimeoutError:
            # 超时清理
            await self.manager.cleanup_tcp_request(conn_id)
            # 通知客户端关闭
            try:
                await conn.websocket.send_text(
                    dump_payload(tcp_close_payload(conn_id))
                )
            except Exception:
                pass
            elapsed = asyncio.get_event_loop().time() - start_time
            return ForwardResponse(
                status=504,
                error="TCP forward timeout",
                duration_ms=int(elapsed * 1000),
            )
        except Exception as e:
            await self.manager.cleanup_tcp_request(conn_id)
            logger.error(f"TCP forward error: {e}", exc_info=True)
            elapsed = asyncio.get_event_loop().time() - start_time
            return ForwardResponse(
                status=500,
                error=str(e),
                duration_ms=int(elapsed * 1000),
            )

    @staticmethod
    def _parse_tcp_response(data: bytes) -> dict:
        """
        尝试将 TCP 响应数据解析为结构化格式

        如果数据是 HTTP 响应格式（HTTP/1.x STATUS ...），解析为状态码 + 头 + body。
        如果是 JSON，直接解析。
        否则作为原始文本返回。
        """
        if not data:
            return {"status": 200, "body": ""}

        text = data.decode("utf-8", errors="replace")

        # 尝试解析为 JSON
        try:
            body = json.loads(text)
            return {"status": 200, "body": body}
        except (json.JSONDecodeError, ValueError):
            pass

        # 尝试解析为 HTTP 响应
        if text.startswith("HTTP/"):
            try:
                # 分离头和 body
                header_end = text.find("\r\n\r\n")
                if header_end == -1:
                    header_end = text.find("\n\n")
                    sep_len = 2
                else:
                    sep_len = 4

                if header_end != -1:
                    header_part = text[:header_end]
                    body_part = text[header_end + sep_len:]

                    # 解析状态行
                    lines = header_part.split("\r\n" if "\r\n" in header_part else "\n")
                    status_line = lines[0]
                    parts = status_line.split(" ", 2)
                    status_code = int(parts[1]) if len(parts) >= 2 else 200

                    # 解析头
                    headers = {}
                    for line in lines[1:]:
                        if ":" in line:
                            key, value = line.split(":", 1)
                            headers[key.strip()] = value.strip()

                    return {
                        "status": status_code,
                        "headers": headers,
                        "body": body_part,
                    }
            except Exception:
                pass

        # 原始文本
        return {"status": 200, "body": text}

    async def forward_stream(
        self,
        domain: str,
        method: str = "POST",
        path: str = "/",
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout: float = 1800.0,
    ) -> AsyncIterator[StreamStartMessage | StreamChunkMessage | StreamEndMessage]:
        """
        转发请求到隧道并返回流式响应（SSE 支持）

        这个方法用于处理 SSE (Server-Sent Events) 响应。
        返回一个 AsyncIterator，依次产生：
        1. StreamStartMessage - 流开始，包含 HTTP 状态码和响应头
        2. StreamChunkMessage* - 零个或多个数据块
        3. StreamEndMessage - 流结束，包含统计信息

        如果目标不是 SSE 响应，将收到一个包含完整响应的 StreamChunkMessage，
        然后立即收到 StreamEndMessage。

        协议 v2 chunked_http（T3）：本方法发出的 TunnelRequest 带
        stream_ok=True——客户端据此允许对非 SSE 大响应切流式回传
        （StreamStart/Chunk*/End），且仅限本请求（/forward、/t/ 缓冲分支
        恒发 stream_ok=False，永远不会收到流式回答）。

        Args:
            domain: 目标隧道域名
            method: HTTP 方法
            path: 请求路径
            headers: 请求头
            body: 请求体
            timeout: 超时时间（秒）

        Yields:
            StreamStartMessage | StreamChunkMessage | StreamEndMessage

        Example:
            async for msg in tunnel_server.forward_stream("agent-001", path="/api/chat", body={"message": "hi"}):
                if isinstance(msg, StreamStartMessage):
                    print(f"Stream started: status={msg.status}")
                elif isinstance(msg, StreamChunkMessage):
                    print(f"Chunk: {msg.data}")
                elif isinstance(msg, StreamEndMessage):
                    print(f"Stream ended: {msg.total_chunks} chunks")
        """
        conn = self.manager.get_connection_by_domain(domain)
        if not conn:
            # 隧道未连接，生成错误响应
            yield StreamStartMessage(id="error", status=503, headers={})
            yield StreamEndMessage(id="error", error=f"Tunnel not connected: {domain}")
            return

        # path 安全校验（@-SSRF）：与 forward() 同源风险，直接拒绝
        if not is_valid_forward_path(path):
            yield StreamStartMessage(id="error", status=400, headers={})
            yield StreamEndMessage(id="error", error="Invalid path: must start with '/'")
            return

        request_id = str(uuid.uuid4())
        request_body_json = json.dumps(body) if body is not None else None

        try:
            # 创建流式请求
            pending = await self.manager.create_stream_request(request_id, domain=domain)

            # 发送请求（数据面快速路径：手工 dict，wire 键集不变）。
            # stream_ok=True（协议 v2 chunked_http，T3）：放行客户端对非 SSE
            # 大响应切流式回传；缓冲 API 不带该标记，行为不变。
            await conn.websocket.send_text(
                dump_payload(
                    request_payload(
                        request_id,
                        method,
                        path,
                        headers,
                        request_body_json,
                        timeout,
                        stream_ok=True,
                    )
                )
            )

            # 从队列中读取流式数据
            # 0.7.3：失败信号（队列溢出/断连/显式失败）统一经队列哨兵传递，
            # 消费侧单 await queue.get() 即可，不再每 chunk 创建一对 task
            start_time = datetime.now()
            while True:
                try:
                    message = await asyncio.wait_for(
                        pending.queue.get(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    # 超时，发送错误结束消息
                    yield StreamEndMessage(
                        id=request_id,
                        error="Stream timeout",
                    )
                    break

                if message is _STREAM_FAILED:
                    # 流已被生产侧标记失败（队列溢出 / 隧道断连 / 显式失败）
                    yield StreamEndMessage(
                        id=request_id,
                        error=pending.error or "stream failed",
                    )
                    break

                if message is None:
                    # 流结束
                    break

                yield message

                if isinstance(message, StreamEndMessage):
                    break

            # 更新统计（内存增量，随流量统计周期批量落库）
            if pending.started:
                self._count_tunnel_request(domain)

        except Exception as e:
            logger.error(f"Stream forward error: {e}", exc_info=True)
            yield StreamEndMessage(
                id=request_id,
                error=str(e),
            )
        finally:
            # 清理流式请求
            await self.manager.cleanup_stream_request(request_id)

    # ============== TCP 监听端口场景 ==============

    def _count_tunnel_bytes(self, domain: str, direction: str, n: int) -> None:
        """累计每隧道流量（内存态，重启归零）"""
        stats = self._tunnel_bytes.setdefault(
            domain, {"bytes_in": 0, "bytes_out": 0}
        )
        stats[direction] += n

    def _count_tunnel_request(self, domain: str) -> None:
        """累计每隧道请求数（内存态增量，随流量统计同周期落库）"""
        self._request_counters[domain] = self._request_counters.get(domain, 0) + 1

    def _live_request_count(self, domain: str | None, db_value: int) -> int:
        """live 请求计数 = DB 落库值 + 未落库内存增量"""
        return db_value + self._request_counters.get(domain or "", 0)

    def _tunnel_byte_stats(self, domain: str | None) -> dict[str, int]:
        stats = self._tunnel_bytes.get(domain or "", {})
        return {"bytes_in": stats.get("bytes_in", 0), "bytes_out": stats.get("bytes_out", 0)}

    # ============== 流量统计持久化 ==============

    async def _seed_tunnel_bytes(self) -> None:
        """从 DB 行恢复流量统计内存计数与落库快照（live 值跨重启连续）"""
        if not self.db:
            return
        try:
            async with self.db.session() as session:
                repo = TunnelRepository(session)
                tunnels = await repo.list_all(limit=999999)
            for t in tunnels:
                if t.bytes_in or t.bytes_out:
                    stats = {"bytes_in": int(t.bytes_in), "bytes_out": int(t.bytes_out)}
                    self._tunnel_bytes[t.domain] = dict(stats)
                    self._flushed_bytes[t.domain] = stats
        except Exception as e:
            logger.warning(f"恢复流量统计初值失败（从 0 开始）: {e}")

    def _start_bytes_flush_task(self) -> None:
        """启动流量统计周期落库任务"""
        self._bytes_flush_task = asyncio.create_task(self._bytes_flush_loop())

    async def _bytes_flush_loop(self) -> None:
        """周期性把流量增量与请求计数写库；DB 不可用时静默跳过（不刷屏，下轮重试）"""
        while True:
            await asyncio.sleep(_BYTES_FLUSH_INTERVAL)
            try:
                await self._flush_tunnel_bytes()
            except Exception as e:
                logger.debug(f"流量统计落库失败（下轮重试）: {e}")
            try:
                await self._flush_request_counters()
            except Exception as e:
                logger.debug(f"请求计数落库失败（下轮重试）: {e}")

    async def _flush_tunnel_bytes(self) -> None:
        """
        把「上次快照以来的流量增量」累加写库（快照法，可安全重复触发）

        只写有正增量的行；每个域名写库成功后立即推进该域快照——
        中途某域失败只影响该域（下轮重发该域增量），不影响其他域，
        也不会像整批推进那样在异常后整批重发导致重复累加。
        """
        if not self.db:
            return

        for domain, stats in list(self._tunnel_bytes.items()):
            flushed = self._flushed_bytes.get(
                domain, {"bytes_in": 0, "bytes_out": 0}
            )
            delta_in = stats["bytes_in"] - flushed["bytes_in"]
            delta_out = stats["bytes_out"] - flushed["bytes_out"]
            if delta_in <= 0 and delta_out <= 0:
                continue

            try:
                async with self.db.session() as session:
                    repo = TunnelRepository(session)
                    await repo.increment_tunnel_bytes(domain, delta_in, delta_out)
            except Exception as e:
                # 该域快照不推进，下轮重试；不阻塞其他域名落库
                logger.warning(f"流量统计落库失败（下轮重试）: domain={domain}, error={e}")
                continue

            flushed = self._flushed_bytes.setdefault(
                domain, {"bytes_in": 0, "bytes_out": 0}
            )
            flushed["bytes_in"] += delta_in
            flushed["bytes_out"] += delta_out

    async def _flush_request_counters(self) -> None:
        """
        把内存中的请求计数增量批量写库

        只有「写库成功」才扣减内存增量（写库期间新到的增量保留，下轮再发）；
        某域失败不影响其他域。
        """
        if not self.db:
            return

        for domain, delta in list(self._request_counters.items()):
            if delta <= 0:
                continue
            try:
                async with self.db.session() as session:
                    repo = TunnelRepository(session)
                    await repo.increment_requests_by_domain(domain, delta)
            except Exception as e:
                logger.warning(
                    f"请求计数落库失败（下轮重试）: domain={domain}, error={e}"
                )
                continue
            self._request_counters[domain] -= delta
            if self._request_counters[domain] <= 0:
                self._request_counters.pop(domain, None)

    # ============== 请求日志后台落库 ==============

    def _enqueue_request_log(self, **kwargs: Any) -> None:
        """请求日志入队（非阻塞）；写库由后台 worker 串行消费

        队列满说明写库速度跟不上转发，丢弃本条并计数——观测数据
        不能反压转发面。丢弃量经 /metrics 的
        tunely_request_logs_dropped_total 暴露。
        """
        if not self.config.request_log_enabled or self._log_queue is None:
            return
        try:
            self._log_queue.put_nowait(kwargs)
        except asyncio.QueueFull:
            self._request_logs_dropped += 1
            logger.warning(
                f"请求日志队列已满，丢弃本条（累计 "
                f"{self._request_logs_dropped}）: domain={kwargs.get('domain')}"
            )

    async def _request_log_worker(self) -> None:
        """请求日志写库 worker：逐条消费队列，单条失败不中断"""
        assert self._log_queue is not None
        while True:
            item = await self._log_queue.get()
            if item is None:
                break
            try:
                await self._write_request_log(**item)
            except Exception as e:
                logger.warning(
                    f"请求日志落库失败（忽略）: domain={item.get('domain')}, error={e}"
                )

    async def _request_log_retention_loop(self) -> None:
        """请求日志保留清理循环：启动即清一轮，之后周期清理"""
        while True:
            try:
                await self._sweep_request_logs()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"请求日志清理失败（下轮重试）: {e}")
            await asyncio.sleep(_LOG_RETENTION_SWEEP_INTERVAL)

    async def _sweep_request_logs(self) -> int:
        """删除超过保留期的请求日志，返回删除行数（0 = 未启用或无超期行）"""
        if not self.db or self.config.request_log_retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.config.request_log_retention_days
        )
        async with self.db.session() as session:
            log_repo = TunnelRequestLogRepository(session)
            deleted = await log_repo.delete_older_than(cutoff)
        if deleted:
            logger.info(
                f"请求日志清理: 删除 {deleted} 条（早于 {cutoff.isoformat()}）"
            )
        return deleted

    # ============== Prometheus 指标 ==============

    @staticmethod
    def _escape_prometheus_label(value: str) -> str:
        """转义 label 值中的特殊字符（防 label 注入）"""
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
        )

    def _render_prometheus_metrics(self, registered: int = 0) -> str:
        """手工拼装 Prometheus 文本格式（不引入第三方依赖）"""
        lines: list[str] = []

        def emit(name: str, mtype: str, help_text: str, samples: list[tuple[str, int]]):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            for labels, value in samples:
                lines.append(f"{name}{labels} {value}")

        emit(
            "tunely_tunnels_registered",
            "gauge",
            "Total number of registered tunnels",
            [("", registered)],
        )
        emit(
            "tunely_tunnels_connected",
            "gauge",
            "Number of tunnels with a connected client",
            [("", len(self.manager.list_connected_domains()))],
        )
        emit(
            "tunely_tcp_connections_active",
            "gauge",
            "Active external TCP connections per tunnel domain",
            [
                (f'{{domain="{self._escape_prometheus_label(d)}"}}', self.manager.count_tcp_connections(d))
                for d in self.manager.list_tcp_connection_domains()
            ],
        )
        emit(
            "tunely_udp_sessions_active",
            "gauge",
            "Active UDP sessions per tunnel domain",
            [
                (f'{{domain="{self._escape_prometheus_label(d)}"}}', self.manager.count_udp_sessions(d))
                for d in self.manager.list_udp_session_domains()
            ],
        )
        emit(
            "tunely_tunnel_bytes_in",
            "counter",
            "Bytes forwarded from external to tunnel client (cumulative)",
            [
                (f'{{domain="{self._escape_prometheus_label(d)}"}}', stats.get("bytes_in", 0))
                for d, stats in sorted(self._tunnel_bytes.items())
            ],
        )
        emit(
            "tunely_tunnel_bytes_out",
            "counter",
            "Bytes forwarded from tunnel client to external (cumulative)",
            [
                (f'{{domain="{self._escape_prometheus_label(d)}"}}', stats.get("bytes_out", 0))
                for d, stats in sorted(self._tunnel_bytes.items())
            ],
        )
        emit(
            "tunely_request_logs_dropped_total",
            "counter",
            "Request logs dropped because the background write queue was full",
            [("", self._request_logs_dropped)],
        )
        emit(
            "tunely_request_logs_queued",
            "gauge",
            "Request logs waiting in the background write queue",
            [
                (
                    "",
                    self._log_queue.qsize()
                    if self._log_queue is not None
                    else 0,
                )
            ],
        )

        return "\n".join(lines) + "\n"

    def _resolve_tcp_listen_specs(self) -> list[tuple[int, str, str | None]]:
        """
        汇总 TCP 监听器配置，返回 [(port, host, domain | None), ...]

        两个来源（同端口时 WS_TUNNEL_TCP_LISTEN 优先）：
        - tcp_listen: "port:domain[,port:domain...]"，每端口固定绑定一条隧道
        - tcp_listen_port + tcp_listen_host + tcp_target_domain：旧字段，
          domain 可为空（运行时回退到第一个在线隧道）
        """
        specs: dict[int, tuple[int, str, str | None]] = {}
        if self.config.tcp_listen:
            for entry in self.config.tcp_listen.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    port_str, domain = entry.split(":", 1)
                    port = int(port_str)
                except ValueError:
                    logger.warning(f"忽略无法解析的监听配置项: {entry!r}")
                    continue
                specs[port] = (port, self.config.tcp_listen_host, domain.strip())
        if self.config.tcp_listen_port:
            specs.setdefault(
                self.config.tcp_listen_port,
                (
                    self.config.tcp_listen_port,
                    self.config.tcp_listen_host,
                    self.config.tcp_target_domain,
                ),
            )
        return list(specs.values())

    async def _start_tcp_listeners(self) -> None:
        """
        启动全部 TCP 监听器

        每个监听器可固定绑定一条隧道（按端口路由）；
        未绑定域名的监听器回退到第一个在线隧道（旧行为）。
        """
        specs = self._resolve_tcp_listen_specs()
        if not specs:
            return

        for port, host, domain in specs:
            try:
                tcp_server = await asyncio.start_server(
                    self._make_tcp_handler(port),
                    host=host,
                    port=port,
                )
            except OSError as e:
                # F22：端口冲突给可读报错（含 host:port 与归属域），
                # 不再裸 OSError traceback
                if e.errno == errno.EADDRINUSE:
                    raise RuntimeError(
                        f"TCP 监听端口被占用，无法绑定 {host}:{port}"
                        f"（归属隧道: {domain or '<首个在线隧道>'}）。"
                        f"请换端口或停掉占用该端口的进程。"
                    ) from e
                raise
            self._tcp_servers.append(tcp_server)
            if domain:
                self._listener_domains[port] = domain
            logger.info(f"TCP 监听器已启动: {host}:{port} -> {domain or '<首个在线隧道>'}")

    def _make_tcp_handler(self, port: int):
        """为指定监听端口生成连接回调（携带端口以便路由到绑定隧道）"""

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._handle_tcp_connection(reader, writer, port=port)

        return handler

    async def _handle_tcp_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int | None = None
    ) -> None:
        """
        处理外部 TCP 连接

        流程:
        1. 接受外部 TCP 连接
        2. 找到目标隧道客户端（按监听端口绑定，否则首个在线隧道）
        3. 发送 TcpConnectMessage 通知客户端建立到目标的连接
        4. 双向转发数据: 外部 TCP <-> WebSocket <-> 客户端 <-> 目标服务
        """
        import base64

        conn_id = str(uuid.uuid4())
        peer = writer.get_extra_info("peername")
        logger.info(f"收到 TCP 连接: {peer} -> conn_id={conn_id}")

        # 确定目标域名：优先该监听端口绑定的隧道
        domain = self._listener_domains.get(port) if port is not None else None
        if not domain:
            domain = self.config.tcp_target_domain
        if not domain:
            # 如果没有配置目标域名，尝试使用第一个在线的隧道
            active = self.manager.list_connected_domains()
            if active:
                domain = active[0]

        if not domain:
            logger.warning(f"没有可用的隧道域名，关闭 TCP 连接: {conn_id}")
            writer.close()
            return

        # 获取隧道连接
        tunnel_conn = self.manager.get_connection_by_domain(domain)
        if not tunnel_conn:
            logger.warning(f"隧道未连接: {domain}，关闭 TCP 连接: {conn_id}")
            writer.close()
            return

        # 每隧道 TCP 并发上限（0 = 不限制）
        max_tcp = self.config.tcp_max_connections
        if max_tcp > 0 and self.manager.count_tcp_connections(domain) >= max_tcp:
            logger.warning(
                f"隧道 {domain} TCP 并发连接已达上限 ({max_tcp})，拒绝新连接: {conn_id}"
            )
            writer.close()
            return

        # 注册 TCP 连接
        await self.manager.register_tcp_connection(
            conn_id=conn_id,
            domain=domain,
            reader=reader,
            writer=writer,
            websocket=tunnel_conn.websocket,
        )

        try:
            # 通知客户端建立到目标的 TCP 连接
            await tunnel_conn.websocket.send_text(
                dump_payload(tcp_connect_payload(conn_id))
            )

            # 启动从外部 TCP 读取数据的任务
            tcp_conn = await self.manager.get_tcp_connection(conn_id)
            if tcp_conn:
                tcp_conn.read_task = asyncio.create_task(
                    self._tcp_read_loop(conn_id, reader, tunnel_conn.websocket, domain)
                )
                # 等待读取任务完成（连接关闭或出错）
                await tcp_conn.read_task

        except Exception as e:
            logger.error(f"TCP 连接处理错误: conn_id={conn_id}, {e}")
        finally:
            # 通知客户端关闭连接。仅当移除动作发生在本协程时才回发：
            # 客户端（目标侧）先发起的关闭会经 WS 消息循环移除连接并取消
            # 读任务，本协程的 finally 随之触发——此时客户端早已清理本地
            # 映射，回发 tcp_close 只会被其当「未知连接」丢弃（churn 期
            # keepalive 空闲连接批量回收时即成回声风暴）。
            if await self.manager.remove_tcp_connection(conn_id):
                try:
                    await tunnel_conn.websocket.send_text(
                        dump_payload(tcp_close_payload(conn_id))
                    )
                except Exception:
                    pass

    async def _tcp_read_loop(
        self,
        conn_id: str,
        reader: asyncio.StreamReader,
        websocket: WebSocket,
        domain: str | None = None,
    ) -> None:
        """
        持续从外部 TCP 连接读取数据，通过 WebSocket 发送给客户端
        """
        import base64

        sequence = 0
        # 空闲超时：回收「连上不发数据」的慢连接（0 = 不启用）
        idle_timeout = self.config.tcp_idle_timeout or None
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65536), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"TCP 连接空闲超时 ({self.config.tcp_idle_timeout}s)，关闭: conn_id={conn_id}"
                    )
                    break
                if not data:
                    # 对端关闭连接
                    logger.info(f"TCP 连接对端关闭: conn_id={conn_id}")
                    break

                if domain:
                    self._count_tunnel_bytes(domain, "bytes_out", len(data))

                # 协议 v2 binary_frames（T2）：该隧道连接协商了该能力时发 WS binary
                # 帧（去 base64+JSON 开销）。能力从 ActiveConnection（经 manager 按
                # domain 查询）取——断线/被接管后查不到时回落 JSON 路径（0.7.x wire
                # 不变）并记 debug 日志。
                conn = self.manager.get_connection_by_domain(domain) if domain else None
                if conn is not None and "binary_frames" in conn.capabilities:
                    await websocket.send_bytes(encode_tcp_data_frame(conn_id, data))
                else:
                    if conn is None:
                        logger.debug(
                            f"binary_frames 未启用（查不到 ActiveConnection），"
                            f"回落 JSON 帧: conn_id={conn_id}"
                        )
                    # 编码并发送（数据面快速路径：手工 dict，wire 键集不变）
                    await websocket.send_text(
                        dump_payload(
                            tcp_data_payload(
                                conn_id,
                                base64.b64encode(data).decode("ascii"),
                                sequence,
                            )
                        )
                    )
                sequence += 1
                logger.debug(f"TCP->WS: conn_id={conn_id}, size={len(data)}, seq={sequence}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"TCP 读取错误: conn_id={conn_id}, {e}")

    # ============== UDP 监听器与会话（协议 v2 udp，docs/PROTOCOL_V2.md §5） ==============

    def _resolve_udp_listen_specs(self) -> list[tuple[int, str]]:
        """解析 WS_TUNNEL_UDP_LISTEN="port:domain[,port:domain...]"（同端口后项覆盖）"""
        specs: dict[int, str] = {}
        if self.config.udp_listen:
            for entry in self.config.udp_listen.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    port_str, domain = entry.split(":", 1)
                    port = int(port_str)
                except ValueError:
                    logger.warning(f"忽略无法解析的 UDP 监听配置项: {entry!r}")
                    continue
                specs[port] = domain.strip()
        return list(specs.items())

    async def _start_udp_listeners(self) -> None:
        """启动全部 UDP 监听器（未配置 udp_listen 时为空操作——默认不开）"""
        specs = self._resolve_udp_listen_specs()
        if not specs:
            return

        host = self.config.tcp_listen_host
        for port, domain in specs:
            queue: asyncio.Queue = asyncio.Queue(maxsize=_UDP_QUEUE_MAXSIZE)
            try:
                transport, _protocol = await asyncio.get_event_loop().create_datagram_endpoint(
                    lambda q=queue, p=port, d=domain: _UdpDatagramProtocol(self, p, d, q),
                    local_addr=(host, port),
                )
            except OSError as e:
                # F22 同款：端口冲突给可读报错（UDP/TCP 同端口号可并存，
                # 但 UDP 端口自身被占用时仍需报错）
                if e.errno == errno.EADDRINUSE:
                    raise RuntimeError(
                        f"UDP 监听端口被占用，无法绑定 {host}:{port}/udp"
                        f"（归属隧道: {domain or '<首个在线隧道>'}）。"
                        f"请换端口或停掉占用该端口的进程。"
                    ) from e
                raise
            self._udp_transports[port] = transport
            self._udp_listener_domains[port] = domain
            self._udp_queues[port] = queue
            pump = asyncio.create_task(self._udp_pump_loop(port, domain, queue))
            self._udp_pump_tasks.append(pump)
            logger.info(f"UDP 监听器已启动: {host}:{port}/udp -> {domain or '<首个在线隧道>'}")

        # 空闲会话回收 sweeper（udp_session_timeout=0 = 不限时不启动）
        if self.config.udp_session_timeout > 0:
            self._udp_sweeper_task = asyncio.create_task(self._udp_session_sweeper_loop())

    async def _udp_pump_loop(self, port: int, domain: str, queue: asyncio.Queue) -> None:
        """UDP 收包 pump：顺序消费收包队列（单消费者保证 udp_open/帧发送有序）"""
        while True:
            item = await queue.get()
            if item is None:
                break
            data, addr = item
            try:
                await self._handle_udp_datagram(port, domain, addr, data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"UDP 包处理错误: port={port}, addr={addr}, {e}")

    async def _handle_udp_datagram(
        self, port: int, domain: str, addr: tuple, data: bytes
    ) -> None:
        """外部 UDP 数据报落地：首包建会话（udp_open JSON + 0x03 帧），后续直接帧

        会话键 = (监听端口, 外部 addr)。门控：隧道未连接或未协商 udp → 丢弃 +
        warning（旧客户端 × 新服务端行为 = 无此流量）。上限：udp_max_sessions
        （每隧道，0 = 不限）超限丢包（防会话表被扫爆 / 反射放大）。
        """
        conn = self.manager.get_connection_by_domain(domain)
        if conn is None or "udp" not in conn.capabilities:
            logger.warning(
                f"丢弃 UDP 包（隧道 {domain} 未连接或未协商 udp）: "
                f"port={port}, addr={addr}, size={len(data)}"
            )
            return

        key = (port, tuple(addr))
        session = self.manager.get_udp_session_by_addr(key)
        if session is None:
            max_sessions = self.config.udp_max_sessions
            if max_sessions > 0 and self.manager.count_udp_sessions(domain) >= max_sessions:
                logger.warning(
                    f"隧道 {domain} UDP 会话数已达上限 ({max_sessions})，丢包: "
                    f"port={port}, addr={addr}"
                )
                return
            session_id = str(uuid.uuid4())
            session = UdpSessionState(
                session_id=session_id, domain=domain, port=port, addr=tuple(addr)
            )
            # 先登记会话再发送：后续同源包（同 pump 顺序）走「已有会话」分支，
            # 保证 udp_open 严格先于首个数据帧到达客户端
            self.manager.add_udp_session(key, session)
            try:
                await conn.websocket.send_text(dump_payload(udp_open_payload(session_id)))
                await conn.websocket.send_bytes(encode_udp_data_frame(session_id, data))
            except Exception as e:
                self.manager.remove_udp_session(key)
                logger.warning(f"udp_open 发送失败，会话回滚: session_id={session_id}, {e}")
                return
            logger.info(f"新建 UDP 会话: session_id={session_id}, domain={domain}, addr={addr}")
        else:
            session.last_seen = datetime.now()
            try:
                await conn.websocket.send_bytes(
                    encode_udp_data_frame(session.session_id, data)
                )
            except Exception as e:
                logger.warning(f"udp_data 帧发送失败: session_id={session.session_id}, {e}")
                return
        self._count_tunnel_bytes(domain, "bytes_out", len(data))

    async def _handle_udp_data_frame(
        self, conn: ActiveConnection, tunnel_domain: str | None, payload: bytes
    ) -> None:
        """客户端 → 外部方向 udp_data 二进制帧落地（协议 v2 udp，能力门控）"""
        if "udp" not in conn.capabilities:
            logger.warning("未协商 udp，收到 udp_data 二进制帧已丢弃")
            return
        try:
            session_id, data = decode_udp_data_frame(payload)
        except ValueError as e:
            logger.warning(f"丢弃畸形 udp 帧: {e}")
            return
        session = self.manager.get_udp_session(session_id)
        if session is None:
            # 会话已被 sweeper 回收 / 客户端主动关闭 / 从未存在：debug 丢弃（UDP 语义）
            logger.debug(f"udp_data 无对应会话，丢弃: session_id={session_id}")
            return
        if self._reject_cross_tunnel_message(
            "udp_data", session_id, session.domain, tunnel_domain
        ):
            return
        transport = self._udp_transports.get(session.port)
        if transport is None or transport.is_closing():
            logger.debug(f"UDP 监听器已关闭，丢弃: session_id={session_id}")
            return
        transport.sendto(data, session.addr)
        session.last_seen = datetime.now()
        self._count_tunnel_bytes(session.domain, "bytes_in", len(data))

    async def _handle_udp_close_from_client(
        self, message: UdpCloseMessage, tunnel_domain: str | None
    ) -> None:
        """客户端主动关闭 UDP 会话：删映射（客户端侧 socket 由它自己回收）"""
        session = self.manager.get_udp_session(message.session_id)
        if session is None:
            logger.debug(f"udp_close 无对应会话（可能已被回收）: session_id={message.session_id}")
            return
        if self._reject_cross_tunnel_message(
            "udp_close", message.session_id, session.domain, tunnel_domain
        ):
            return
        self.manager.remove_udp_session((session.port, session.addr))
        logger.info(
            f"客户端关闭 UDP 会话: session_id={message.session_id}, domain={session.domain}, "
            f"reason={message.reason or 'n/a'}"
        )

    async def _udp_session_sweeper_loop(self) -> None:
        """UDP 空闲会话回收循环：周期扫描 last_seen 超时的会话"""
        timeout = self.config.udp_session_timeout
        # 扫描间隔：不超过 30s，也不小于 1s（timeout 很小时按 1s 粒度回收）
        interval = max(1.0, min(timeout, 30.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._sweep_udp_sessions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"UDP 会话回收失败（下轮重试）: {e}")

    async def _sweep_udp_sessions(self) -> int:
        """回收空闲超时的 UDP 会话（发 udp_close + 删映射），返回回收数

        独立方法便于测试：把 udp_session_timeout 调小后可直接调用触发回收，
        不必等 sweeper 周期。
        """
        timeout = self.config.udp_session_timeout
        if timeout <= 0:
            return 0
        now = datetime.now()
        expired = [
            (key, session)
            for key, session in self.manager.iter_udp_sessions()
            if (now - session.last_seen).total_seconds() > timeout
        ]
        for key, session in expired:
            self.manager.remove_udp_session(key)
            await self._send_udp_close(session, reason="idle timeout")
        if expired:
            logger.info(f"UDP 空闲会话回收: {len(expired)} 个（timeout={timeout}s）")
        return len(expired)

    async def _send_udp_close(self, session: UdpSessionState, reason: str | None = None) -> None:
        """向会话归属隧道下发 udp_close（尽力而为：连接不在/发送失败仅记日志）"""
        conn = self.manager.get_connection_by_domain(session.domain)
        if conn is None:
            return
        try:
            await conn.websocket.send_text(
                dump_payload(udp_close_payload(session.session_id, reason))
            )
        except Exception as e:
            logger.debug(
                f"udp_close 发送失败（忽略）: session_id={session.session_id}, {e}"
            )

    # ============== TCP 模式支持方法（WebSocket 消息处理） ==============

    async def _handle_tcp_data_from_client(self, message: TcpDataMessage) -> None:
        """
        处理从客户端接收的 TCP 数据（JSON 形态，base64）

        解 base64 后与 binary_frames 帧路径共用 _route_tcp_payload 落地。
        """
        import base64

        try:
            data = base64.b64decode(message.data)
            await self._route_tcp_payload(message.conn_id, data)
        except Exception as e:
            logger.error(f"处理 TCP 数据错误: {message.conn_id}, {e}")

    async def _route_tcp_payload(self, conn_id: str, data: bytes) -> None:
        """
        tcp_data 落地路由（JSON TcpDataMessage 与 binary 帧两路共用）

        两种场景:
        1. HTTP 触发的 TCP 转发 -> 累积到 PendingTcpRequest
        2. 服务端 TCP 监听 -> 写入到真实 TCP 连接
        """
        # 优先检查是否有待响应的 HTTP 触发的 TCP 转发
        if await self.manager.handle_tcp_response_data(conn_id, data):
            logger.debug(f"TCP 响应数据累积: conn_id={conn_id}, size={len(data)}")
            return

        # 其次检查是否有真实 TCP 连接（服务端监听场景）
        success = await self.manager.handle_tcp_data(conn_id, data)
        if success:
            tcp_conn = await self.manager.get_tcp_connection(conn_id)
            if tcp_conn and tcp_conn.domain:
                self._count_tunnel_bytes(tcp_conn.domain, "bytes_in", len(data))
        else:
            # 并发关闭竞态下在途数据竞到移除之后，属正常时序，debug 防刷屏
            logger.debug(f"无法路由 TCP 数据: conn_id={conn_id}")

    async def _handle_tcp_close_from_client(self, message: TcpCloseMessage) -> None:
        """
        处理从客户端接收的 TCP 关闭消息

        两种场景:
        1. HTTP 触发的 TCP 转发 -> 完成 PendingTcpRequest（解析 Future）
        2. 服务端 TCP 监听 -> 关闭真实 TCP 连接
        """
        # 优先检查是否有待响应的 HTTP 触发的 TCP 请求
        if await self.manager.complete_tcp_request(message.conn_id, error=message.error):
            logger.info(f"TCP 请求已完成: conn_id={message.conn_id}")
            return

        # 其次关闭真实 TCP 连接。返回 False = 外部关闭路径已先移除
        # （目标侧先断时客户端关闭与外部 EOF 的并发竞态），降为 debug
        # 防止 churn 期刷屏
        if await self.manager.remove_tcp_connection(message.conn_id):
            logger.info(f"客户端请求关闭 TCP 连接: {message.conn_id}")
        else:
            logger.debug(f"忽略重复/迟到的 tcp_close: {message.conn_id}")
