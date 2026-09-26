"""
隧道模式（mode: http/tcp）端到端贯通测试

对应 issue #2：mode 曾在任何 API/CLI 面上都设不上也读不出，
请求里的未知字段被 Pydantic 静默丢弃。

覆盖：
- 请求模型校验（mode 取值、extra="forbid" 拒绝未知字段）
- Repository 持久化（默认 http、显式 tcp）
- 服务端 API（创建回显、查询可见、更新可改）
- forward 转发路径按 mode 分派（tcp -> _forward_tcp）
"""

import pytest
from pydantic import ValidationError
from unittest.mock import AsyncMock, MagicMock

from tunely.config import TunnelServerConfig
from tunely.database import DatabaseManager
from tunely.repository import TunnelRepository
from tunely.server import (
    CreateTunnelRequest,
    TunnelServer,
    UpdateTunnelRequest,
)


# ============== 请求模型校验 ==============


class TestCreateTunnelRequestValidation:
    def test_mode_defaults_to_http(self):
        req = CreateTunnelRequest(domain="d.example")
        assert req.mode == "http"

    def test_accepts_tcp_mode(self):
        req = CreateTunnelRequest(domain="d.example", mode="tcp")
        assert req.mode == "tcp"

    def test_rejects_invalid_mode(self):
        with pytest.raises(ValidationError):
            CreateTunnelRequest(domain="d.example", mode="grpc")

    def test_rejects_unknown_fields(self):
        """extra="forbid"：服务端未实现的字段必须报错而非静默丢弃（issue #2 的根因）"""
        with pytest.raises(ValidationError):
            CreateTunnelRequest(domain="d.example", moden="tcp")


class TestUpdateTunnelRequestValidation:
    def test_mode_optional(self):
        assert UpdateTunnelRequest().mode is None
        assert UpdateTunnelRequest(mode="tcp").mode == "tcp"

    def test_rejects_invalid_mode(self):
        with pytest.raises(ValidationError):
            UpdateTunnelRequest(mode="websocket")

    def test_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            UpdateTunnelRequest(unexpected="x")


# ============== Repository 持久化 ==============


class TestRepositoryMode:
    @pytest.mark.asyncio
    async def test_default_mode_is_http(self, db_manager: DatabaseManager):
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.create(domain="mode-default")

            assert tunnel.mode == "http"

    @pytest.mark.asyncio
    async def test_create_with_tcp_mode(self, db_manager: DatabaseManager):
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.create(domain="mode-tcp", mode="tcp")
            await session.commit()

            assert tunnel.mode == "tcp"

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.get_by_domain("mode-tcp")

            assert tunnel is not None
            assert tunnel.mode == "tcp"


# ============== 服务端 API ==============


@pytest.fixture
async def server():
    """初始化了内存数据库的 TunnelServer"""
    srv = TunnelServer(
        config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
    )
    await srv.initialize()
    yield srv
    await srv.close()


class TestServerModeApi:
    @pytest.mark.asyncio
    async def test_create_echoes_and_persists_mode(self, server: TunnelServer):
        request = CreateTunnelRequest(domain="api-tcp", name="TCP", mode="tcp")
        response = await server._create_tunnel(request, api_key=None)

        assert response.mode == "tcp"

        info = await server._get_tunnel("api-tcp", api_key=None)
        assert info.mode == "tcp"

    @pytest.mark.asyncio
    async def test_create_default_mode_visible_in_list(self, server: TunnelServer):
        request = CreateTunnelRequest(domain="api-http")
        response = await server._create_tunnel(request, api_key=None)

        assert response.mode == "http"

        tunnels = await server._list_tunnels(api_key=None)
        assert [t.mode for t in tunnels if t.domain == "api-http"] == ["http"]

    @pytest.mark.asyncio
    async def test_update_mode(self, server: TunnelServer):
        await server._create_tunnel(
            CreateTunnelRequest(domain="api-switch"), api_key=None
        )

        updated = await server._update_tunnel(
            "api-switch", UpdateTunnelRequest(mode="tcp"), api_key=None
        )
        assert updated.mode == "tcp"

        updated = await server._update_tunnel(
            "api-switch", UpdateTunnelRequest(mode="http"), api_key=None
        )
        assert updated.mode == "http"


class TestForwardModeDispatch:
    @pytest.mark.asyncio
    async def test_tcp_mode_routes_to_forward_tcp(self, server: TunnelServer):
        await server._create_tunnel(
            CreateTunnelRequest(domain="dispatch", mode="tcp"), api_key=None
        )
        # 0.7.0 起 forward 按 ActiveConnection.mode 缓存路由，注册时带上 mode
        await server.manager.register(
            websocket=MagicMock(),
            tunnel_id=1,
            domain="dispatch",
            token="tok",
            mode="tcp",
        )

        server._forward_tcp = AsyncMock(return_value="tcp")
        server._forward_http = AsyncMock(return_value="http")

        result = await server.forward(
            domain="dispatch", method="GET", path="/", headers={}, body=None, timeout=5
        )

        assert result == "tcp"
        server._forward_tcp.assert_awaited_once()
        server._forward_http.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_http_mode_routes_to_forward_http(self, server: TunnelServer):
        await server._create_tunnel(
            CreateTunnelRequest(domain="dispatch-http"), api_key=None
        )
        await server.manager.register(
            websocket=MagicMock(),
            tunnel_id=1,
            domain="dispatch-http",
            token="tok",
        )

        server._forward_tcp = AsyncMock(return_value="tcp")
        server._forward_http = AsyncMock(return_value="http")

        result = await server.forward(
            domain="dispatch-http",
            method="GET",
            path="/",
            headers={},
            body=None,
            timeout=5,
        )

        assert result == "http"
        server._forward_http.assert_awaited_once()
        server._forward_tcp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forward_uses_cached_mode_after_db_row_deleted(self, server: TunnelServer):
        """0.7.0：forward 用 ActiveConnection.mode 缓存；DB 行删除后连接仍在时沿用缓存"""
        await server._create_tunnel(
            CreateTunnelRequest(domain="cache-tcp", mode="tcp"), api_key=None
        )
        await server.manager.register(
            websocket=MagicMock(),
            tunnel_id=1,
            domain="cache-tcp",
            token="tok",
            mode="tcp",
        )
        # 删除 DB 行（token 轮换/隧道吊销后连接可能仍存活）
        await server._delete_tunnel("cache-tcp", api_key=None)

        server._forward_tcp = AsyncMock(return_value="tcp")
        server._forward_http = AsyncMock(return_value="http")

        result = await server.forward(
            domain="cache-tcp", method="GET", path="/", headers={}, body=None, timeout=5
        )

        assert result == "tcp"
        server._forward_tcp.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_register_rejects_unknown_mode(self, server: TunnelServer):
        """register 收到未知 mode 时回退 http（防御 MagicMock 等异常值）"""
        await server.manager.register(
            websocket=MagicMock(),
            tunnel_id=1,
            domain="mode-guard",
            token="tok",
            mode="grpc",
        )
        conn = server.manager.get_connection_by_token("tok")
        assert conn is not None
        assert conn.mode == "http"
