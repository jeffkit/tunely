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
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
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
    StreamStartMessage,
    StreamChunkMessage,
    StreamEndMessage,
    parse_message,
)
from .repository import TunnelRepository, TunnelRequestLogRepository

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
    enabled: bool
    connected: bool
    token: str | None = None  # 可选，仅在需要时返回
    created_at: str | None = None
    last_connected_at: str | None = None
    total_requests: int = 0


class UpdateTunnelRequest(BaseModel):
    """更新隧道请求"""

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None


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

        # request_id → PendingRequest（普通响应）
        self._pending_requests: dict[str, PendingRequest] = {}

        # request_id → PendingStreamRequest（流式响应/SSE）
        self._pending_stream_requests: dict[str, PendingStreamRequest] = {}

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
                    # 尝试 ping 检查连接是否存活
                    # WebSocket 的 client_state 可以告诉我们连接状态
                    is_healthy = old_conn.websocket.client_state.name == "CONNECTED"
                except Exception:
                    is_healthy = False
                
                if is_healthy and not force:
                    # 旧连接健康且不强制抢占，拒绝新连接
                    logger.warning(f"拒绝新连接: domain={domain}，已有活跃连接")
                    return (False, f"已有活跃连接存在，使用 --force 参数可强制抢占")
                
                # 关闭旧连接（不健康或强制抢占）
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
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._update_tunnel(domain, request, x_api_key)

        @self.router.delete("/api/tunnels/{domain}")
        async def delete_tunnel(
            domain: str,
            x_api_key: str | None = Header(None, alias="x-api-key"),
            x_tunnel_token: str | None = Header(None, alias="x-tunnel-token"),
        ):
            return await self._delete_tunnel(domain, x_api_key, x_tunnel_token)

        @self.router.post(
            "/api/tunnels/{domain}/regenerate-token", response_model=RegenerateTokenResponse
        )
        async def regenerate_token(
            domain: str,
            x_api_key: str | None = Header(None, alias="x-api-key"),
        ):
            return await self._regenerate_token(domain, x_api_key)

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

        @self.router.get("/api/info")
        async def get_server_info():
            """获取服务信息和域名配置规则"""
            # 使用配置的 ws_url，或自动生成
            ws_url = self.config.ws_url or f"wss://{self.config.domain}{self.config.ws_path}"
            
            result = {
                "name": "Tunely Server",
                "version": "0.2.0",
                "domain": {
                    "pattern": f"{{subdomain}}.{self.config.domain}",
                    "customizable": "subdomain",
                    "suffix": f".{self.config.domain}",
                },
                "websocket": {
                    "url": ws_url,
                },
                "protocols": ["https", "http"],
            }
            
            # 添加 instruction 字段（如果配置了）
            if self.config.instruction:
                result["instruction"] = self.config.instruction
            
            return result

    def _check_admin_api_key(self, api_key: str | None) -> None:
        """检查管理 API 密钥"""
        if self.config.admin_api_key:
            if api_key != self.config.admin_api_key:
                raise HTTPException(status_code=401, detail="Invalid API key")

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

                # 尝试注册连接
                force = getattr(message, 'force', False)
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

                # 发送认证成功
                await websocket.send_text(
                    AuthOkMessage(
                        domain=tunnel.domain,
                        tunnel_id=str(tunnel.id),
                    ).model_dump_json()
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
                # 流式消息处理（SSE 支持）
                elif isinstance(message, StreamStartMessage):
                    await self.manager.handle_stream_start(message)
                elif isinstance(message, StreamChunkMessage):
                    await self.manager.handle_stream_chunk(message)
                elif isinstance(message, StreamEndMessage):
                    await self.manager.handle_stream_end(message)
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
        """创建隧道 - 公开接口,不需要 API Key"""
        # 移除 API Key 验证,允许用户自助创建隧道
        # self._check_admin_api_key(api_key)

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

            await session.commit()
            await session.refresh(tunnel)

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

    async def _update_tunnel(
        self, domain: str, request: UpdateTunnelRequest, api_key: str | None
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

    async def _regenerate_token(
        self, domain: str, api_key: str | None
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
        tunnel_token: str | None = None
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

            return ForwardResponse(
                status=response.status,
                headers=response.headers,
                body=json.loads(response.body) if response.body else None,
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

    async def forward_stream(
        self,
        domain: str,
        method: str = "POST",
        path: str = "/",
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout: float = 300.0,
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
