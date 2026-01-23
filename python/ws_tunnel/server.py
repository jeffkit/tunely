"""
WS-Tunnel 服务端 SDK

提供 WebSocket 隧道服务端功能，可嵌入到 FastAPI 应用中

使用示例:
    from fastapi import FastAPI
    from ws_tunnel import TunnelServer

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
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .config import TunnelServerConfig
from .database import DatabaseManager
from .protocol import (
    AuthErrorMessage,
    AuthMessage,
    AuthOkMessage,
    MessageType,
    PingMessage,
    PongMessage,
    TunnelRequest,
    TunnelResponse,
    parse_message,
)
from .repository import TunnelRepository

logger = logging.getLogger(__name__)


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
    """待响应的请求"""

    request_id: str
    future: asyncio.Future
    created_at: datetime = field(default_factory=datetime.now)


# ============== 请求/响应模型 ==============


class CreateTunnelRequest(BaseModel):
    """创建隧道请求"""

    domain: str
    name: str | None = None
    description: str | None = None


class CreateTunnelResponse(BaseModel):
    """创建隧道响应"""

    domain: str
    token: str
    name: str | None = None


class TunnelInfo(BaseModel):
    """隧道信息"""

    domain: str
    name: str | None = None
    description: str | None = None
    enabled: bool
    connected: bool
    created_at: str | None = None
    last_connected_at: str | None = None
    total_requests: int = 0


class ForwardRequest(BaseModel):
    """转发请求"""

    method: str = "POST"
    path: str = "/"
    headers: dict[str, str] = {}
    body: Any = None
    timeout: float = 300.0


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

        # request_id → PendingRequest
        self._pending_requests: dict[str, PendingRequest] = {}

        self._lock = asyncio.Lock()

    async def register(
        self,
        websocket: WebSocket,
        tunnel_id: int,
        domain: str,
        token: str,
    ) -> None:
        """注册隧道连接"""
        async with self._lock:
            # 如果已有连接，先关闭旧连接
            if token in self._connections:
                old_conn = self._connections[token]
                try:
                    await old_conn.websocket.close(code=1000, reason="New connection")
                except Exception:
                    pass
                logger.info(f"关闭旧连接: domain={domain}")

            conn = ActiveConnection(
                websocket=websocket,
                tunnel_id=tunnel_id,
                domain=domain,
                token=token,
            )
            self._connections[token] = conn
            self._domain_token_map[domain] = token

            logger.info(f"隧道已连接: domain={domain}")

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
        """创建待响应的请求"""
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = PendingRequest(
            request_id=request_id,
            future=future,
        )
        return future

    async def complete_request(self, request_id: str, response: TunnelResponse) -> bool:
        """完成请求"""
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
        return False

    async def update_heartbeat(self, token: str) -> None:
        """更新心跳时间"""
        conn = self._connections.get(token)
        if conn:
            conn.last_heartbeat = datetime.now()


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

        # 注册路由
        self._register_routes()

    async def initialize(self) -> None:
        """初始化服务器"""
        self.db = DatabaseManager(self.config.database_url)
        await self.db.initialize()
        logger.info("TunnelServer 初始化完成")

    async def close(self) -> None:
        """关闭服务器"""
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
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._create_tunnel(request, x_api_key)

        @self.router.get("/api/tunnels", response_model=list[TunnelInfo])
        async def list_tunnels(
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._list_tunnels(x_api_key)

        @self.router.get("/api/tunnels/{domain}", response_model=TunnelInfo)
        async def get_tunnel(
            domain: str,
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._get_tunnel(domain, x_api_key)

        @self.router.delete("/api/tunnels/{domain}")
        async def delete_tunnel(
            domain: str,
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._delete_tunnel(domain, x_api_key)

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

    def _check_admin_api_key(self, api_key: str | None) -> None:
        """检查管理 API 密钥"""
        if self.config.admin_api_key:
            if api_key != self.config.admin_api_key:
                raise HTTPException(status_code=401, detail="Invalid API key")

    async def _handle_websocket(self, websocket: WebSocket) -> None:
        """处理 WebSocket 连接"""
        await websocket.accept()

        token: str | None = None
        tunnel_domain: str | None = None

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

                # 发送认证成功
                await websocket.send_text(
                    AuthOkMessage(
                        domain=tunnel.domain,
                        tunnel_id=str(tunnel.id),
                    ).model_dump_json()
                )

                # 注册连接
                await self.manager.register(
                    websocket=websocket,
                    tunnel_id=tunnel.id,
                    domain=tunnel.domain,
                    token=token,
                )

            # 处理消息循环
            while True:
                raw_message = await websocket.receive_text()
                data = json.loads(raw_message)
                message = parse_message(data)

                if isinstance(message, PongMessage):
                    await self.manager.update_heartbeat(token)
                elif isinstance(message, TunnelResponse):
                    await self.manager.complete_request(message.id, message)
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
            if token:
                await self.manager.unregister(token)

    async def _create_tunnel(
        self, request: CreateTunnelRequest, api_key: str | None
    ) -> CreateTunnelResponse:
        """创建隧道"""
        self._check_admin_api_key(api_key)

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
            )

            return CreateTunnelResponse(
                domain=tunnel.domain,
                token=tunnel.token,
                name=tunnel.name,
            )

    async def _list_tunnels(self, api_key: str | None) -> list[TunnelInfo]:
        """列出所有隧道"""
        self._check_admin_api_key(api_key)

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)
            tunnels = await repo.list_all()

            return [
                TunnelInfo(
                    domain=t.domain,
                    name=t.name,
                    description=t.description,
                    enabled=t.enabled,
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
                enabled=tunnel.enabled,
                connected=self.manager.is_connected(tunnel.domain),
                created_at=tunnel.created_at.isoformat() if tunnel.created_at else None,
                last_connected_at=(
                    tunnel.last_connected_at.isoformat() if tunnel.last_connected_at else None
                ),
                total_requests=tunnel.total_requests,
            )

    async def _delete_tunnel(self, domain: str, api_key: str | None) -> dict:
        """删除隧道"""
        self._check_admin_api_key(api_key)

        if not self.db:
            raise HTTPException(status_code=500, detail="Database not initialized")

        async with self.db.session() as session:
            repo = TunnelRepository(session)
            deleted = await repo.delete(domain)

            if not deleted:
                raise HTTPException(status_code=404, detail="Tunnel not found")

            return {"success": True, "domain": domain}

    async def forward(
        self,
        domain: str,
        method: str = "POST",
        path: str = "/",
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout: float = 300.0,
    ) -> ForwardResponse:
        """
        转发请求到隧道

        Args:
            domain: 目标隧道域名
            method: HTTP 方法
            path: 请求路径
            headers: 请求头
            body: 请求体
            timeout: 超时时间（秒）

        Returns:
            ForwardResponse
        """
        conn = self.manager.get_connection_by_domain(domain)
        if not conn:
            return ForwardResponse(
                status=503,
                error=f"Tunnel not connected: {domain}",
            )

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
            response = await asyncio.wait_for(future, timeout=timeout)

            # 更新统计
            if self.db:
                async with self.db.session() as session:
                    repo = TunnelRepository(session)
                    await repo.increment_requests(conn.token)

            return ForwardResponse(
                status=response.status,
                headers=response.headers,
                body=json.loads(response.body) if response.body else None,
                duration_ms=response.duration_ms,
                error=response.error,
            )

        except asyncio.TimeoutError:
            await self.manager.fail_request(request_id, "Request timeout")
            return ForwardResponse(
                status=504,
                error="Request timeout",
            )
        except Exception as e:
            await self.manager.fail_request(request_id, str(e))
            return ForwardResponse(
                status=500,
                error=str(e),
            )
