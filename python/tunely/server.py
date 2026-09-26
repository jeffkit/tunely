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
import hashlib
import hmac
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    parse_message,
)
from .repository import TunnelRepository, TunnelRequestLogRepository, AdminAuditLogRepository

logger = logging.getLogger(__name__)

# 认证失败断开前的延迟（秒）：提高 token 暴力尝试成本（仅认证失败路径，
# 业务拒绝路径如 disabled / already connected 不受影响）
_AUTH_FAILURE_DELAY = 1.0

# 流量统计落库周期（秒）：把内存计数增量累加写回 tunnels 表
_BYTES_FLUSH_INTERVAL = 30.0


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


# ============== 数据结构 ==============


@dataclass
class ActiveConnection:
    """活跃的隧道连接"""

    websocket: WebSocket
    tunnel_id: int
    domain: str
    token: str
    connected_at: datetime = field(default_factory=datetime.now)
    last_heartbeat: datetime = field(default_factory=datetime.now)


@dataclass
class PendingRequest:
    """待响应的请求（普通响应）"""

    request_id: str
    future: asyncio.Future
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class PendingStreamRequest:
    """待响应的流式请求（SSE 支持）"""

    request_id: str
    queue: asyncio.Queue  # 存储流式数据块
    started: bool = False
    ended: bool = False
    start_message: StreamStartMessage | None = None
    end_message: StreamEndMessage | None = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class TcpConnectionState:
    """TCP 连接状态（服务端有真实 TCP 连接时使用）"""

    conn_id: str
    domain: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    read_task: asyncio.Task | None = None
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
    created_at: datetime = field(default_factory=datetime.now)


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

    def __init__(self):
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

        self._lock = asyncio.Lock()

    async def register(
        self,
        websocket: WebSocket,
        tunnel_id: int,
        domain: str,
        token: str,
        force: bool = False,
    ) -> tuple[bool, str | None]:
        """
        注册隧道连接
        
        Args:
            websocket: WebSocket 连接
            tunnel_id: 隧道 ID
            domain: 隧道域名
            token: 隧道令牌
            force: 是否强制抢占已有连接
            
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
            )
            self._connections[token] = conn
            self._domain_token_map[domain] = token

            logger.info(f"隧道已连接: domain={domain}")
            return (True, None)

    async def unregister(self, token: str) -> None:
        """注销隧道连接"""
        async with self._lock:
            conn = self._connections.pop(token, None)
            if conn:
                self._domain_token_map.pop(conn.domain, None)
                logger.info(f"隧道已断开: domain={conn.domain}")

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

    def list_connected_domains(self) -> list[str]:
        """列出所有已连接的域名"""
        return list(self._domain_token_map.keys())

    async def create_pending_request(self, request_id: str) -> asyncio.Future:
        """创建待响应的请求（普通响应）"""
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = PendingRequest(
            request_id=request_id,
            future=future,
        )
        return future

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
        stream_pending = self._pending_stream_requests.pop(request_id, None)
        if stream_pending:
            await stream_pending.queue.put(None)  # 发送结束信号
            return True
        return False

    async def update_heartbeat(self, token: str) -> None:
        """更新心跳时间"""
        conn = self._connections.get(token)
        if conn:
            conn.last_heartbeat = datetime.now()

    # ============== 流式请求支持（SSE） ==============

    async def create_stream_request(self, request_id: str) -> PendingStreamRequest:
        """创建待响应的流式请求"""
        pending = PendingStreamRequest(
            request_id=request_id,
            queue=asyncio.Queue(),
        )
        self._pending_stream_requests[request_id] = pending
        return pending

    async def handle_stream_start(self, message: StreamStartMessage) -> bool:
        """处理流式响应开始"""
        pending = self._pending_stream_requests.get(message.id)
        if pending:
            pending.started = True
            pending.start_message = message
            await pending.queue.put(message)
            return True
        return False

    async def handle_stream_chunk(self, message: StreamChunkMessage) -> bool:
        """处理流式数据块"""
        pending = self._pending_stream_requests.get(message.id)
        if pending and pending.started and not pending.ended:
            await pending.queue.put(message)
            return True
        return False

    async def handle_stream_end(self, message: StreamEndMessage) -> bool:
        """处理流式响应结束"""
        pending = self._pending_stream_requests.get(message.id)
        if pending:
            pending.ended = True
            pending.end_message = message
            await pending.queue.put(message)
            await pending.queue.put(None)  # 发送结束信号
            # 注意：不立即删除，等迭代器完成后再清理
            return True
        return False

    async def cleanup_stream_request(self, request_id: str) -> None:
        """清理流式请求"""
        self._pending_stream_requests.pop(request_id, None)

    # ============== TCP 模式支持 ==============

    async def register_tcp_connection(
        self,
        conn_id: str,
        domain: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        websocket: WebSocket,
    ) -> None:
        """注册 TCP 连接"""
        tcp_conn = TcpConnectionState(
            conn_id=conn_id,
            domain=domain,
            reader=reader,
            writer=writer,
            websocket=websocket,
        )
        self._tcp_connections[conn_id] = tcp_conn
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
            if tcp_conn.writer:
                try:
                    tcp_conn.writer.close()
                except Exception:
                    pass
        for tcp_conn in states:
            if tcp_conn.writer:
                try:
                    await tcp_conn.writer.wait_closed()
                except Exception:
                    pass
        self._tcp_connections.clear()
        if states:
            logger.info(f"已关闭 {len(states)} 个活跃 TCP 连接")

    async def remove_tcp_connection(self, conn_id: str) -> None:
        """移除 TCP 连接"""
        tcp_conn = self._tcp_connections.pop(conn_id, None)
        if tcp_conn:
            logger.info(f"移除 TCP 连接: {conn_id}")
            # 取消读取任务
            if tcp_conn.read_task:
                tcp_conn.read_task.cancel()
            # 关闭 writer
            if tcp_conn.writer and not tcp_conn.closed:
                try:
                    tcp_conn.writer.close()
                    await tcp_conn.writer.wait_closed()
                except Exception as e:
                    logger.error(f"关闭 TCP writer 错误: {e}")

    async def handle_tcp_data(self, conn_id: str, data: bytes) -> bool:
        """处理 TCP 数据（写入到真实 TCP 连接 - 服务端监听场景）"""
        tcp_conn = self._tcp_connections.get(conn_id)
        if not tcp_conn or tcp_conn.closed:
            logger.warning(f"TCP 连接不存在或已关闭: {conn_id}")
            return False

        try:
            tcp_conn.writer.write(data)
            await tcp_conn.writer.drain()
            return True
        except Exception as e:
            logger.error(f"写入 TCP 数据失败: {conn_id}, {e}")
            tcp_conn.closed = True
            await self.remove_tcp_connection(conn_id)
            return False

    # ============== TCP Pending Request（HTTP 触发的 TCP 转发） ==============

    async def create_pending_tcp_request(self, conn_id: str) -> asyncio.Future:
        """创建待响应的 TCP 请求"""
        future = asyncio.get_event_loop().create_future()
        self._pending_tcp_requests[conn_id] = PendingTcpRequest(
            conn_id=conn_id,
            future=future,
        )
        return future

    async def handle_tcp_response_data(self, conn_id: str, data: bytes) -> bool:
        """累积客户端返回的 TCP 响应数据"""
        pending = self._pending_tcp_requests.get(conn_id)
        if not pending:
            return False
        pending.chunks.append(data)
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


# ============== 隧道服务器 ==============


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
        self.manager = TunnelManager()
        self.router = APIRouter(tags=["Tunnel"])
        self._tcp_servers: list[asyncio.Server] = []
        # 监听端口 -> 绑定的隧道域名（多监听器路由）
        self._listener_domains: dict[int, str] = {}
        # 每隧道流量统计（内存态计数，周期性落库）：domain -> {"bytes_in": n, "bytes_out": n}
        # bytes_in = 外部 → 内网服务；bytes_out = 内网服务 → 外部
        self._tunnel_bytes: dict[str, dict[str, int]] = {}
        # 已落库快照（initialize 时从 DB 行 seed），flush 只写「当前值 - 快照」的增量
        self._flushed_bytes: dict[str, dict[str, int]] = {}
        self._bytes_flush_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()

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

        # 启动流量统计周期落库任务
        self._start_bytes_flush_task()

    async def close(self) -> None:
        """关闭服务器"""
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
            
            try:
                from importlib.metadata import version as _pkg_version
                server_version = _pkg_version("tunely")
            except Exception:
                server_version = "unknown"
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

    def _check_admin_api_key(self, api_key: str | None) -> None:
        """检查管理 API 密钥（常数时间比较，防时序侧信道）"""
        if self.config.admin_api_key:
            provided = api_key or ""
            if not hmac.compare_digest(provided, self.config.admin_api_key):
                raise HTTPException(status_code=401, detail="Invalid API key")

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

                # 更新最后连接时间
                await repo.update_last_connected(token)

                # 尝试注册连接（记录注册前是否已有连接，用于 takeover 审计）
                force = getattr(message, 'force', False)
                had_existing = self.manager.get_connection_by_token(token) is not None
                success, error = await self.manager.register(
                    websocket=websocket,
                    tunnel_id=tunnel.id,
                    domain=tunnel.domain,
                    token=token,
                    force=force,
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
                    ).model_dump_json()
                )

                # 发送连接 webhook（fire-and-forget）
                task = asyncio.create_task(self._notify_connected(tunnel.domain))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            # 处理消息循环
            while True:
                raw_message = await websocket.receive_text()
                data = json.loads(raw_message)
                message = parse_message(data)

                if isinstance(message, PongMessage):
                    await self.manager.update_heartbeat(token)
                elif isinstance(message, TunnelResponse):
                    await self.manager.complete_request(message.id, message)
                # 流式消息处理（SSE 支持）
                elif isinstance(message, StreamStartMessage):
                    await self.manager.handle_stream_start(message)
                elif isinstance(message, StreamChunkMessage):
                    await self.manager.handle_stream_chunk(message)
                elif isinstance(message, StreamEndMessage):
                    await self.manager.handle_stream_end(message)
                # TCP 消息处理
                elif isinstance(message, TcpDataMessage):
                    await self._handle_tcp_data_from_client(message)
                elif isinstance(message, TcpCloseMessage):
                    await self._handle_tcp_close_from_client(message)
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
                await self.manager.unregister(token)

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
                    total_requests=t.total_requests,
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
                    tunnel.last_connected_at.isoformat() if tunnel.last_connected_at else None
                ),
                total_requests=tunnel.total_requests,
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
                    tunnel.last_connected_at.isoformat() if tunnel.last_connected_at else None
                ),
                total_requests=tunnel.total_requests,
            )

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

            await self._record_audit(
                "regenerate", domain=domain, source_ip=source_ip
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

        # 审计必须在删除会话提交之后再写：否则 SQLite 下同库第二会话
        # 会被未提交的写锁阻塞，审计静默丢失（0.6.0 实测）
        await self._record_audit(
            "delete",
            domain=domain,
            detail="via=tunnel-token" if tunnel_token else None,
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
        # 检查连接
        conn = self.manager.get_connection_by_domain(domain)
        if not conn:
            return ForwardResponse(
                status=503,
                error=f"Tunnel not connected: {domain}",
            )

        # 查询隧道模式
        tunnel_mode = "http"  # 默认 HTTP 模式（向后兼容）
        if self.db:
            async with self.db.session() as session:
                repo = TunnelRepository(session)
                tunnel = await repo.get_by_domain(domain)
                if tunnel:
                    tunnel_mode = tunnel.mode

        # 根据模式选择转发方式
        if tunnel_mode == "tcp":
            return await self._forward_tcp(domain, body, timeout)
        else:
            return await self._forward_http(domain, method, path, headers, body, timeout)

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
        request = TunnelRequest(
            id=request_id,
            method=method,
            path=path,
            headers=headers or {},
            body=json.dumps(body) if body else None,
            timeout=timeout,
        )

        try:
            # 创建 Future 等待响应
            future = await self.manager.create_pending_request(request_id)

            # 发送请求
            await conn.websocket.send_text(request.model_dump_json())

            # 等待响应
            start_time = asyncio.get_event_loop().time()
            response = await asyncio.wait_for(future, timeout=timeout)
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # 更新统计和记录日志
            if self.db:
                async with self.db.session() as session:
                    tunnel_repo = TunnelRepository(session)
                    await tunnel_repo.increment_requests(conn.token)
                    
                    # 记录请求日志
                    log_repo = TunnelRequestLogRepository(session)
                    try:
                        response_body_str = None
                        if response.body:
                            try:
                                response_body_str = json.dumps(json.loads(response.body))
                            except:
                                response_body_str = str(response.body)[:10000]
                        
                        request_body_str = None
                        if body:
                            try:
                                request_body_str = json.dumps(body)
                            except:
                                request_body_str = str(body)[:10000]
                        
                        await log_repo.create(
                            tunnel_domain=domain,
                            method=method,
                            path=path,
                            request_headers=headers,
                            request_body=request_body_str,
                            status_code=response.status,
                            response_headers=response.headers,
                            response_body=response_body_str,
                            error=response.error,
                            duration_ms=duration_ms,
                        )
                        await session.commit()
                    except Exception as e:
                        # 日志记录失败不应该影响请求处理
                        logger.warning(f"记录请求日志失败: {e}")
                        await session.rollback()

            # Parse body: try JSON first, fall back to raw string
            parsed_body = None
            if response.body:
                try:
                    parsed_body = json.loads(response.body)
                except (json.JSONDecodeError, ValueError):
                    parsed_body = response.body

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
            if self.db:
                async with self.db.session() as session:
                    log_repo = TunnelRequestLogRepository(session)
                    try:
                        request_body_str = None
                        if body:
                            try:
                                request_body_str = json.dumps(body)
                            except:
                                request_body_str = str(body)[:10000]
                        
                        await log_repo.create(
                            tunnel_domain=domain,
                            method=method,
                            path=path,
                            request_headers=headers,
                            request_body=request_body_str,
                            status_code=504,
                            error=error_msg,
                            duration_ms=int(timeout * 1000),
                        )
                        await session.commit()
                    except Exception as e:
                        logger.warning(f"记录请求日志失败: {e}")
                        await session.rollback()
            
            return ForwardResponse(
                status=504,
                error=error_msg,
            )
        except Exception as e:
            error_msg = str(e)
            await self.manager.fail_request(request_id, error_msg)
            
            # 记录错误日志
            if self.db:
                async with self.db.session() as session:
                    log_repo = TunnelRequestLogRepository(session)
                    try:
                        request_body_str = None
                        if body:
                            try:
                                request_body_str = json.dumps(body)
                            except:
                                request_body_str = str(body)[:10000]
                        
                        await log_repo.create(
                            tunnel_domain=domain,
                            method=method,
                            path=path,
                            request_headers=headers,
                            request_body=request_body_str,
                            status_code=500,
                            error=error_msg,
                            duration_ms=0,
                        )
                        await session.commit()
                    except Exception as e:
                        logger.warning(f"记录请求日志失败: {e}")
                        await session.rollback()
            
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
            future = await self.manager.create_pending_tcp_request(conn_id)

            # 2. 发送 TCP 连接建立消息
            connect_msg = TcpConnectMessage(conn_id=conn_id)
            await conn.websocket.send_text(connect_msg.model_dump_json())

            # 3. 发送数据
            if body:
                # 处理不同类型的 body
                if isinstance(body, bytes):
                    data = body
                elif isinstance(body, str):
                    data = body.encode("utf-8")
                else:
                    data = json.dumps(body).encode("utf-8")

                # 编码为 base64 并发送
                data_msg = TcpDataMessage(
                    conn_id=conn_id,
                    data=base64.b64encode(data).decode("ascii"),
                    sequence=0,
                )
                await conn.websocket.send_text(data_msg.model_dump_json())

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
                close_msg = TcpCloseMessage(conn_id=conn_id)
                await conn.websocket.send_text(close_msg.model_dump_json())
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

        request_id = str(uuid.uuid4())
        request = TunnelRequest(
            id=request_id,
            method=method,
            path=path,
            headers=headers or {},
            body=json.dumps(body) if body else None,
            timeout=timeout,
        )

        try:
            # 创建流式请求
            pending = await self.manager.create_stream_request(request_id)

            # 发送请求
            await conn.websocket.send_text(request.model_dump_json())

            # 从队列中读取流式数据
            start_time = datetime.now()
            while True:
                try:
                    # 使用超时等待
                    message = await asyncio.wait_for(
                        pending.queue.get(),
                        timeout=timeout
                    )
                except asyncio.TimeoutError:
                    # 超时，发送错误结束消息
                    yield StreamEndMessage(
                        id=request_id,
                        error="Stream timeout",
                    )
                    break

                if message is None:
                    # 流结束
                    break

                yield message

                if isinstance(message, StreamEndMessage):
                    break

            # 更新统计
            if self.db and pending.started:
                async with self.db.session() as session:
                    repo = TunnelRepository(session)
                    await repo.increment_requests(conn.token)

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
        """周期性把流量增量写库；DB 不可用时静默跳过（不刷屏，下轮重试）"""
        while True:
            await asyncio.sleep(_BYTES_FLUSH_INTERVAL)
            try:
                await self._flush_tunnel_bytes()
            except Exception as e:
                logger.debug(f"流量统计落库失败（下轮重试）: {e}")

    async def _flush_tunnel_bytes(self) -> None:
        """
        把「上次快照以来的流量增量」累加写库（快照法，可安全重复触发）

        只写有正增量的行；全部写完后推进快照。
        """
        if not self.db:
            return

        deltas: list[tuple[str, int, int]] = []
        for domain, stats in self._tunnel_bytes.items():
            flushed = self._flushed_bytes.get(
                domain, {"bytes_in": 0, "bytes_out": 0}
            )
            delta_in = stats["bytes_in"] - flushed["bytes_in"]
            delta_out = stats["bytes_out"] - flushed["bytes_out"]
            if delta_in > 0 or delta_out > 0:
                deltas.append((domain, delta_in, delta_out))

        if not deltas:
            return

        async with self.db.session() as session:
            repo = TunnelRepository(session)
            for domain, delta_in, delta_out in deltas:
                await repo.increment_tunnel_bytes(domain, delta_in, delta_out)

        for domain, delta_in, delta_out in deltas:
            flushed = self._flushed_bytes.setdefault(
                domain, {"bytes_in": 0, "bytes_out": 0}
            )
            flushed["bytes_in"] += delta_in
            flushed["bytes_out"] += delta_out

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
            tcp_server = await asyncio.start_server(
                self._make_tcp_handler(port),
                host=host,
                port=port,
            )
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
            connect_msg = TcpConnectMessage(conn_id=conn_id)
            await tunnel_conn.websocket.send_text(connect_msg.model_dump_json())

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
            # 通知客户端关闭连接
            try:
                close_msg = TcpCloseMessage(conn_id=conn_id)
                await tunnel_conn.websocket.send_text(close_msg.model_dump_json())
            except Exception:
                pass
            await self.manager.remove_tcp_connection(conn_id)

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
        try:
            while True:
                data = await reader.read(65536)  # 64KB chunks
                if not data:
                    # 对端关闭连接
                    logger.info(f"TCP 连接对端关闭: conn_id={conn_id}")
                    break

                if domain:
                    self._count_tunnel_bytes(domain, "bytes_out", len(data))

                # 编码并发送
                data_msg = TcpDataMessage(
                    conn_id=conn_id,
                    data=base64.b64encode(data).decode("ascii"),
                    sequence=sequence,
                )
                await websocket.send_text(data_msg.model_dump_json())
                sequence += 1
                logger.debug(f"TCP->WS: conn_id={conn_id}, size={len(data)}, seq={sequence}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"TCP 读取错误: conn_id={conn_id}, {e}")

    # ============== TCP 模式支持方法（WebSocket 消息处理） ==============

    async def _handle_tcp_data_from_client(self, message: TcpDataMessage) -> None:
        """
        处理从客户端接收的 TCP 数据

        两种场景:
        1. HTTP 触发的 TCP 转发 -> 累积到 PendingTcpRequest
        2. 服务端 TCP 监听 -> 写入到真实 TCP 连接
        """
        import base64

        try:
            data = base64.b64decode(message.data)

            # 优先检查是否有待响应的 HTTP 触发的 TCP 转发
            if await self.manager.handle_tcp_response_data(message.conn_id, data):
                logger.debug(f"TCP 响应数据累积: conn_id={message.conn_id}, size={len(data)}")
                return

            # 其次检查是否有真实 TCP 连接（服务端监听场景）
            success = await self.manager.handle_tcp_data(message.conn_id, data)
            if success:
                tcp_conn = await self.manager.get_tcp_connection(message.conn_id)
                if tcp_conn and tcp_conn.domain:
                    self._count_tunnel_bytes(tcp_conn.domain, "bytes_in", len(data))
            else:
                logger.warning(f"无法路由 TCP 数据: conn_id={message.conn_id}")
        except Exception as e:
            logger.error(f"处理 TCP 数据错误: {message.conn_id}, {e}")

    async def _handle_tcp_close_from_client(self, message: TcpCloseMessage) -> None:
        """
        处理从客户端接收的 TCP 关闭消息

        两种场景:
        1. HTTP 触发的 TCP 转发 -> 完成 PendingTcpRequest（解析 Future）
        2. 服务端 TCP 监听 -> 关闭真实 TCP 连接
        """
        logger.info(f"客户端请求关闭 TCP 连接: {message.conn_id}")

        # 优先检查是否有待响应的 HTTP 触发的 TCP 请求
        if await self.manager.complete_tcp_request(message.conn_id, error=message.error):
            logger.info(f"TCP 请求已完成: conn_id={message.conn_id}")
            return

        # 其次关闭真实 TCP 连接
        await self.manager.remove_tcp_connection(message.conn_id)
