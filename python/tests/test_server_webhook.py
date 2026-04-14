"""
M1: Tunely Server 客户端连接 Webhook 单元测试

测试 TunnelServer 在客户端认证成功后向 dispatch_webhook_url 发送 POST 通知的行为。
包含对抗性测试：HMAC 认证、success 变量初始化、fire-and-forget 不阻塞握手、并发 webhook。
"""

import asyncio
import hashlib
import hmac
import json
import os
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


@pytest.fixture
def server_with_webhook_secret():
    """创建带有 dispatch_webhook_url 和 dispatch_webhook_secret 的 TunnelServer 实例"""
    config = TunnelServerConfig(
        dispatch_webhook_url="http://localhost:8083",
        dispatch_webhook_secret="test-secret-key",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    return TunnelServer(config=config)


def _make_mock_client(status_code: int = 200):
    """创建标准 mock httpx.AsyncClient"""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestNotifyConnected:
    """测试 _notify_connected 方法"""

    @pytest.mark.asyncio
    async def test_webhook_sent_on_auth_success(self, server_with_webhook):
        """dispatch_webhook_url 已设置时，认证成功后应发送 POST webhook"""
        mock_client = _make_mock_client()

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server_with_webhook._notify_connected("user-b")

        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        assert call_url == "http://localhost:8083/api/tunnel/connected"

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

        mock_client = _make_mock_client()

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
        mock_client = _make_mock_client(202)

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server_with_webhook._notify_connected("my-agent")

        _, kwargs = mock_client.post.call_args
        payload = json.loads(kwargs["content"])
        assert payload["domain"] == "my-agent"
        assert payload["event"] == "connected"

    @pytest.mark.asyncio
    async def test_webhook_timeout_configured(self, server_with_webhook):
        """httpx.AsyncClient 应使用合理的超时（5秒）"""
        mock_client = _make_mock_client()

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client) as mock_cls:
            await server_with_webhook._notify_connected("user-b")

        # 验证 AsyncClient 使用了 timeout 参数
        mock_cls.assert_called_once_with(timeout=5.0)


class TestWebhookHmacAuth:
    """测试 webhook HMAC-SHA256 认证（f2 修复验证 + 对抗性测试）"""

    @pytest.mark.asyncio
    async def test_hmac_signature_added_when_secret_set(self, server_with_webhook_secret):
        """dispatch_webhook_secret 已设置时，请求头应包含正确的 HMAC-SHA256 签名"""
        mock_client = _make_mock_client()

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server_with_webhook_secret._notify_connected("user-b")

        _, kwargs = mock_client.post.call_args
        headers = kwargs["headers"]
        body_bytes = kwargs["content"]

        assert "X-Webhook-Signature" in headers
        sig_header = headers["X-Webhook-Signature"]
        assert sig_header.startswith("sha256=")

        # 验证签名正确性
        expected_digest = hmac.new(
            b"test-secret-key",
            body_bytes,
            hashlib.sha256,
        ).hexdigest()
        assert sig_header == f"sha256={expected_digest}"

    @pytest.mark.asyncio
    async def test_no_signature_when_secret_not_set(self, server_with_webhook):
        """dispatch_webhook_secret 未设置时，不应附加 X-Webhook-Signature 头（向后兼容）"""
        mock_client = _make_mock_client()

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server_with_webhook._notify_connected("user-b")

        _, kwargs = mock_client.post.call_args
        headers = kwargs["headers"]
        assert "X-Webhook-Signature" not in headers

    @pytest.mark.asyncio
    async def test_signature_covers_exact_body_bytes(self, server_with_webhook_secret):
        """HMAC 签名应覆盖实际发送的 body bytes，签名与 body 必须一致"""
        mock_client = _make_mock_client()

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server_with_webhook_secret._notify_connected("special-domain")

        _, kwargs = mock_client.post.call_args
        body_bytes = kwargs["content"]
        sig_header = kwargs["headers"]["X-Webhook-Signature"]

        # 用相同 secret 重新计算，验证签名与 body 严格绑定
        digest = hmac.new(b"test-secret-key", body_bytes, hashlib.sha256).hexdigest()
        assert sig_header == f"sha256={digest}"

        # 篡改 body 后签名不应匹配
        tampered = body_bytes + b" "
        tampered_digest = hmac.new(b"test-secret-key", tampered, hashlib.sha256).hexdigest()
        assert sig_header != f"sha256={tampered_digest}"

    def test_dispatch_webhook_secret_config_default_none(self):
        """dispatch_webhook_secret 默认值应为 None"""
        config = TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        assert config.dispatch_webhook_secret is None

    def test_dispatch_webhook_secret_env_var(self):
        """dispatch_webhook_secret 可通过 WS_TUNNEL_DISPATCH_WEBHOOK_SECRET 环境变量设置"""
        os.environ["WS_TUNNEL_DISPATCH_WEBHOOK_SECRET"] = "my-secret"
        try:
            config = TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
            assert config.dispatch_webhook_secret == "my-secret"
        finally:
            del os.environ["WS_TUNNEL_DISPATCH_WEBHOOK_SECRET"]


class TestSuccessVariableInit:
    """对抗性测试 at1：finally 块中 success 变量初始化问题"""

    @pytest.mark.asyncio
    async def test_handle_websocket_no_name_error_on_db_failure(self):
        """at1: DB session 在 register 前抛出异常时，finally 块不应因 NameError 产生二次异常"""
        from tunely.protocol import AuthMessage

        config = TunnelServerConfig(
            dispatch_webhook_url=None,
            database_url="sqlite+aiosqlite:///:memory:",
        )
        server = TunnelServer(config=config)

        # mock WebSocket
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock()
        mock_ws.close = AsyncMock()

        auth_msg = AuthMessage(token="valid-token")
        mock_ws.receive_text = AsyncMock(return_value=auth_msg.model_dump_json())

        # mock DB session 在 get_by_token 时抛出 OperationalError
        from sqlalchemy.exc import OperationalError

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_repo = AsyncMock()
        mock_repo.get_by_token = AsyncMock(
            side_effect=OperationalError("DB connection failed", None, None)
        )

        mock_db = MagicMock()
        mock_db.session = MagicMock(return_value=mock_session)

        server.db = mock_db

        with patch("tunely.server.TunnelRepository", return_value=mock_repo):
            # 不应因 NameError 或 OperationalError 传播而抛出异常
            # _handle_websocket 内部应捕获并记录错误
            try:
                await server._handle_websocket(mock_ws)
            except Exception as e:
                # OperationalError 从 except Exception 路径被捕获并 log，不再向上传播
                # 若此处出现 NameError，则说明 success 未初始化修复无效
                assert not isinstance(e, NameError), (
                    f"NameError 不应从 finally 块传播: {e}"
                )


class TestWebhookUrlMultiPath:
    """对抗性测试 at3：URL 含多层路径时拼接正确性"""

    @pytest.mark.asyncio
    async def test_url_with_subpath_no_double_slash(self):
        """at3: dispatch_webhook_url 含子路径时，rstrip('/') 应正确处理"""
        config = TunnelServerConfig(
            dispatch_webhook_url="http://localhost:8083/v1/",
            database_url="sqlite+aiosqlite:///:memory:",
        )
        server = TunnelServer(config=config)

        mock_client = _make_mock_client()

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server._notify_connected("user-b")

        call_url = mock_client.post.call_args[0][0]
        assert "//" not in call_url.replace("http://", "").replace("https://", "")
        assert call_url == "http://localhost:8083/v1/api/tunnel/connected"

    @pytest.mark.asyncio
    async def test_url_without_trailing_slash(self):
        """dispatch_webhook_url 无尾部斜杠时，URL 拼接也正确"""
        config = TunnelServerConfig(
            dispatch_webhook_url="http://localhost:8083/v1",
            database_url="sqlite+aiosqlite:///:memory:",
        )
        server = TunnelServer(config=config)

        mock_client = _make_mock_client()

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await server._notify_connected("user-b")

        call_url = mock_client.post.call_args[0][0]
        assert call_url == "http://localhost:8083/v1/api/tunnel/connected"


class TestFireAndForgetNonBlocking:
    """测试 f4：fire-and-forget 不阻塞 WebSocket 握手"""

    @pytest.mark.asyncio
    async def test_auth_ok_sent_before_notify_completed(self, server_with_webhook):
        """f4: AuthOkMessage 应在 _notify_connected 完成前发出（不阻塞握手）"""
        from tunely.protocol import AuthMessage, AuthOkMessage

        call_order = []

        async def slow_notify(domain: str):
            await asyncio.sleep(0.1)
            call_order.append("notify_done")

        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock(
            side_effect=lambda _: call_order.append("auth_ok_sent") or None
        )
        mock_ws.close = AsyncMock()

        auth_msg = AuthMessage(token="valid-token")
        # 第一次返回 auth，第二次模拟 disconnect
        from fastapi import WebSocketDisconnect

        mock_ws.receive_text = AsyncMock(
            side_effect=[auth_msg.model_dump_json(), WebSocketDisconnect()]
        )

        # mock DB 返回有效 tunnel
        mock_tunnel = MagicMock()
        mock_tunnel.id = 1
        mock_tunnel.domain = "user-b"
        mock_tunnel.enabled = True

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_repo = AsyncMock()
        mock_repo.get_by_token = AsyncMock(return_value=mock_tunnel)
        mock_repo.update_last_connected = AsyncMock()

        mock_db = MagicMock()
        mock_db.session = MagicMock(return_value=mock_session)

        server_with_webhook.db = mock_db

        # mock manager.register 返回 (True, None)
        server_with_webhook.manager.register = AsyncMock(return_value=(True, None))
        server_with_webhook.manager.unregister = AsyncMock()

        with (
            patch("tunely.server.TunnelRepository", return_value=mock_repo),
            patch.object(server_with_webhook, "_notify_connected", side_effect=slow_notify),
        ):
            await server_with_webhook._handle_websocket(mock_ws)

        # AuthOkMessage 应先于 notify_done 记录
        assert "auth_ok_sent" in call_order
        # auth_ok_sent 必须在 notify_done 之前（或 notify_done 未出现，因为 task 还没完成）
        if "notify_done" in call_order:
            assert call_order.index("auth_ok_sent") < call_order.index("notify_done")


class TestConcurrentWebhook:
    """测试 f5：并发 webhook 调用的隔离性"""

    @pytest.mark.asyncio
    async def test_concurrent_webhooks_all_complete(self, server_with_webhook):
        """f5: 多个并发 _notify_connected 调用应全部完成，互不干扰"""
        completed = []

        async def counted_post(*args, **kwargs):
            await asyncio.sleep(0.01)
            completed.append(args[0])
            resp = MagicMock()
            resp.status_code = 200
            return resp

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=counted_post)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            await asyncio.gather(
                server_with_webhook._notify_connected("domain-1"),
                server_with_webhook._notify_connected("domain-2"),
                server_with_webhook._notify_connected("domain-3"),
            )

        assert len(completed) == 3

    @pytest.mark.asyncio
    async def test_concurrent_one_failure_no_affect_others(self, server_with_webhook):
        """f5: 一个 webhook 失败不应影响其他并发 webhook"""
        completed = []
        call_count = [0]

        async def selective_fail(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Network error on second call")
            completed.append(args[0])
            resp = MagicMock()
            resp.status_code = 200
            return resp

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=selective_fail)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("tunely.server.httpx.AsyncClient", return_value=mock_client):
            # 不应有任何异常传播（fire-and-forget 吞掉异常）
            await asyncio.gather(
                server_with_webhook._notify_connected("domain-1"),
                server_with_webhook._notify_connected("domain-2"),
                server_with_webhook._notify_connected("domain-3"),
            )

        # 第 1、3 次调用成功，第 2 次失败但被吞掉
        assert len(completed) == 2


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
        os.environ["WS_TUNNEL_DISPATCH_WEBHOOK_URL"] = "http://dispatch:8083"
        try:
            config = TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
            assert config.dispatch_webhook_url == "http://dispatch:8083"
        finally:
            del os.environ["WS_TUNNEL_DISPATCH_WEBHOOK_URL"]
