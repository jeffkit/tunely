"""
服务端测试
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from tunely.server import TunnelManager, TunnelServer
from tunely.config import TunnelServerConfig
from tunely.protocol import TunnelResponse


class TestTunnelManager:
    """测试隧道管理器"""

    @pytest.mark.asyncio
    async def test_register_and_get_connection(self):
        """测试注册和获取连接"""
        manager = TunnelManager()
        mock_ws = MagicMock()

        await manager.register(
            websocket=mock_ws,
            tunnel_id=1,
            domain="test-domain",
            token="test-token",
        )

        conn = manager.get_connection_by_domain("test-domain")
        assert conn is not None
        assert conn.domain == "test-domain"
        assert conn.token == "test-token"

    @pytest.mark.asyncio
    async def test_get_connection_by_token(self):
        """测试根据令牌获取连接"""
        manager = TunnelManager()
        mock_ws = MagicMock()

        await manager.register(
            websocket=mock_ws,
            tunnel_id=1,
            domain="test-domain",
            token="test-token",
        )

        conn = manager.get_connection_by_token("test-token")
        assert conn is not None
        assert conn.domain == "test-domain"

    @pytest.mark.asyncio
    async def test_unregister(self):
        """测试注销连接"""
        manager = TunnelManager()
        mock_ws = MagicMock()

        await manager.register(
            websocket=mock_ws,
            tunnel_id=1,
            domain="test-domain",
            token="test-token",
        )

        await manager.unregister("test-token")

        conn = manager.get_connection_by_domain("test-domain")
        assert conn is None

    @pytest.mark.asyncio
    async def test_is_connected(self):
        """测试连接状态检查"""
        manager = TunnelManager()
        mock_ws = MagicMock()

        assert manager.is_connected("test-domain") is False

        await manager.register(
            websocket=mock_ws,
            tunnel_id=1,
            domain="test-domain",
            token="test-token",
        )

        assert manager.is_connected("test-domain") is True

    @pytest.mark.asyncio
    async def test_list_connected_domains(self):
        """测试列出已连接域名"""
        manager = TunnelManager()

        await manager.register(MagicMock(), 1, "domain-1", "token-1")
        await manager.register(MagicMock(), 2, "domain-2", "token-2")
        await manager.register(MagicMock(), 3, "domain-3", "token-3")

        domains = manager.list_connected_domains()
        assert len(domains) == 3
        assert "domain-1" in domains
        assert "domain-2" in domains
        assert "domain-3" in domains

    @pytest.mark.asyncio
    async def test_create_and_complete_request(self):
        """测试创建和完成请求"""
        manager = TunnelManager()

        future = await manager.create_pending_request("req-001")
        assert not future.done()

        response = TunnelResponse(
            id="req-001",
            status=200,
            headers={},
            body='{"result": "ok"}',
        )

        await manager.complete_request("req-001", response)

        assert future.done()
        result = await future
        assert result.status == 200

    @pytest.mark.asyncio
    async def test_fail_request(self):
        """测试请求失败"""
        manager = TunnelManager()

        future = await manager.create_pending_request("req-002")

        await manager.fail_request("req-002", "Connection lost")

        assert future.done()
        with pytest.raises(Exception):
            await future


class TestTunnelServer:
    """测试隧道服务器"""

    def test_server_init(self):
        """测试服务器初始化"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
        )
        server = TunnelServer(config=config)

        assert server.config == config
        assert server.router is not None

    @pytest.mark.asyncio
    async def test_server_initialize_and_close(self):
        """测试服务器启动和关闭"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
        )
        server = TunnelServer(config=config)

        await server.initialize()
        assert server.db is not None

        await server.close()

    @pytest.mark.asyncio
    async def test_forward_not_connected(self):
        """测试转发到未连接的隧道"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
        )
        server = TunnelServer(config=config)
        await server.initialize()

        response = await server.forward(
            domain="not-connected",
            method="POST",
            path="/api/test",
        )

        assert response.status == 503
        assert "not connected" in response.error.lower()

        await server.close()
