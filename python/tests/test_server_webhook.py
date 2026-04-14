"""
M1: Tunely Server 客户端连接 Webhook 单元测试

测试 TunnelServer 在客户端认证成功后向 dispatch_webhook_url 发送 POST 通知的行为。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tunely.config import TunnelServerConfig
from tunely.server import TunnelServer


@pytest.fixture
def server_with_webhook():
    """创建带有 dispatch_webhook_url 的 TunnelServer 实例"""
    config = TunnelServerConfig(
        dispatch_webhook_url="http://localhost:8083",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    return TunnelServer(config=config)


@pytest.fixture
def server_without_webhook():
    """创建没有 dispatch_webhook_url 的 TunnelServer 实例"""
    config = TunnelServerConfig(
        dispatch_webhook_url=None,
        database_url="sqlite+aiosqlite:///:memory:",
    )
    return TunnelServer(config=config)


class TestNotifyConnected:
    """测试 _notify_connected 方法"""

    @pytest.mark.asyncio
    async def test_webhook_sent_on_auth_success(self, server_with_webhook):
        """dispatch_webhook_url 已设置时，认证成功后应发送 POST webhook"""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server_with_webhook._notify_connected("user-b")

        mock_client.post.assert_called_once_with(
            "http://localhost:8083/api/tunnel/connected",
            json={"domain": "user-b", "event": "connected"},
        )

    @pytest.mark.asyncio
    async def test_webhook_not_sent_when_url_not_set(self, server_without_webhook):
        """dispatch_webhook_url 未设置时，不应发送任何 HTTP 请求"""
        with patch("tunely.server.httpx.AsyncClient") as mock_client_class:
            await server_without_webhook._notify_connected("user-b")

        mock_client_class.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_url_trailing_slash_normalized(self, server_with_webhook):
        """dispatch_webhook_url 末尾的斜杠应被正规化"""
        server_with_webhook.config.dispatch_webhook_url = "http://localhost:8083/"

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server_with_webhook._notify_connected("user-b")

        # URL 不应有双斜杠
        call_url = mock_client.post.call_args[0][0]
        assert call_url == "http://localhost:8083/api/tunnel/connected"

    @pytest.mark.asyncio
    async def test_webhook_failure_does_not_raise(self, server_with_webhook):
        """webhook 发送失败时不应抛出异常（fire-and-forget）"""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            # 不应抛出任何异常
            await server_with_webhook._notify_connected("user-b")

    @pytest.mark.asyncio
    async def test_webhook_payload_contains_domain_and_event(self, server_with_webhook):
        """webhook payload 必须包含 domain 和 event 字段"""
        mock_response = MagicMock()
        mock_response.status_code = 202

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server_with_webhook._notify_connected("my-agent")

        _, kwargs = mock_client.post.call_args
        payload = kwargs["json"]
        assert payload["domain"] == "my-agent"
        assert payload["event"] == "connected"

    @pytest.mark.asyncio
    async def test_webhook_timeout_configured(self, server_with_webhook):
        """httpx.AsyncClient 应使用合理的超时（5秒）"""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client) as mock_cls:
            await server_with_webhook._notify_connected("user-b")

        # 验证 AsyncClient 使用了 timeout 参数
        mock_cls.assert_called_once_with(timeout=5.0)


class TestConfigDispatchWebhookUrl:
    """测试 TunnelServerConfig 的 dispatch_webhook_url 字段"""

    def test_default_is_none(self):
        """默认值应为 None（禁用 webhook）"""
        config = TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        assert config.dispatch_webhook_url is None

    def test_can_be_set(self):
        """能够设置 dispatch_webhook_url"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            dispatch_webhook_url="http://localhost:8083",
        )
        assert config.dispatch_webhook_url == "http://localhost:8083"

    def test_env_var_prefix(self):
        """环境变量前缀为 WS_TUNNEL_"""
        import os

        os.environ["WS_TUNNEL_DISPATCH_WEBHOOK_URL"] = "http://dispatch:8083"
        try:
            config = TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
            assert config.dispatch_webhook_url == "http://dispatch:8083"
        finally:
            del os.environ["WS_TUNNEL_DISPATCH_WEBHOOK_URL"]
