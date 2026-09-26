"""
服务端安全加固测试（0.5.x）

- 管理 API 密钥常数时间比较（hmac.compare_digest）
- WebSocket 认证失败限速（断开前延迟）
- 每隧道 TCP 并发连接上限（tcp_max_connections）
- serve 命令 --api-key 环境变量回退（WS_TUNNEL_ADMIN_API_KEY）
- CORS 默认收紧（空 = 仅同源，不装配 CORSMiddleware）
"""

import asyncio
import hmac
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware

from tunely.app import AppSettings, create_full_app
from tunely.cli import serve
from tunely.config import TunnelServerConfig
from tunely.protocol import AuthMessage, PongMessage
from tunely.server import CreateTunnelRequest, TcpConnectionState, TunnelServer
from tunely.server import TunnelManager


# ============== 任务1：常数时间比较 ==============


class TestConstantTimeApiKeyCompare:
    """_check_admin_api_key 使用 hmac.compare_digest，None 语义不变"""

    def _make_server(self) -> TunnelServer:
        return TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                admin_api_key="secret-key",
            )
        )

    def test_correct_key_passes(self):
        srv = self._make_server()
        srv._check_admin_api_key("secret-key")  # 不抛异常即通过

    def test_wrong_key_rejected_401(self):
        srv = self._make_server()
        with pytest.raises(HTTPException) as exc_info:
            srv._check_admin_api_key("wrong-key")
        assert exc_info.value.status_code == 401

    def test_none_key_rejected_401(self):
        """api_key 为 None（未提供）时语义不变：401"""
        srv = self._make_server()
        with pytest.raises(HTTPException) as exc_info:
            srv._check_admin_api_key(None)
        assert exc_info.value.status_code == 401

    def test_key_not_configured_stays_open(self):
        """未配置 admin api-key 时不做任何检查（内网模式语义不变）"""
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        srv._check_admin_api_key(None)
        srv._check_admin_api_key("anything")

    def test_uses_compare_digest(self):
        """实现应走 hmac.compare_digest（防时序侧信道）"""
        srv = self._make_server()
        with patch(
            "tunely.server.hmac.compare_digest", wraps=hmac.compare_digest
        ) as spy:
            srv._check_admin_api_key("secret-key")
            assert spy.called
            spy.reset_mock()
            with pytest.raises(HTTPException):
                srv._check_admin_api_key("wrong-key")
            assert spy.called


# ============== 任务2：WS 认证失败限速 ==============


def _make_ws(receive_texts: list[str]) -> AsyncMock:
    mock_ws = AsyncMock()
    mock_ws.accept = AsyncMock()
    mock_ws.send_text = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.receive_text = AsyncMock(side_effect=receive_texts)
    return mock_ws


def _make_db_repo(get_by_token_return=None) -> tuple[MagicMock, AsyncMock]:
    mock_repo = AsyncMock()
    mock_repo.get_by_token = AsyncMock(return_value=get_by_token_return)
    mock_repo.update_last_connected = AsyncMock()

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_db = MagicMock()
    mock_db.session = MagicMock(return_value=mock_session)
    return mock_db, mock_repo


class TestAuthFailureDelay:
    """token 无效等认证失败路径：关闭前延迟；业务拒绝路径不受影响"""

    @pytest.mark.asyncio
    async def test_invalid_token_sleeps_before_close(self):
        server = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        mock_db, mock_repo = _make_db_repo(get_by_token_return=None)
        server.db = mock_db

        mock_ws = _make_ws([AuthMessage(token="bad-token").model_dump_json()])

        with (
            patch("tunely.server.TunnelRepository", return_value=mock_repo),
            patch("tunely.server.asyncio.sleep", new_callable=AsyncMock) as fake_sleep,
        ):
            await server._handle_websocket(mock_ws)

        fake_sleep.assert_awaited_once_with(1.0)
        # 先发错误、再延迟、最后关闭
        mock_ws.close.assert_awaited_once_with(code=1008)
        sent = mock_ws.send_text.await_args.args[0]
        assert "Invalid token" in sent

    @pytest.mark.asyncio
    async def test_invalid_token_delay_measured_in_real_time(self):
        """端到端计时验证：无效 token 的处理耗时 >= 延迟值"""
        server = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        mock_db, mock_repo = _make_db_repo(get_by_token_return=None)
        server.db = mock_db

        mock_ws = _make_ws([AuthMessage(token="bad-token").model_dump_json()])

        with patch("tunely.server.TunnelRepository", return_value=mock_repo):
            start = time.monotonic()
            await server._handle_websocket(mock_ws)
            elapsed = time.monotonic() - start

        assert elapsed >= 0.5

    @pytest.mark.asyncio
    async def test_non_auth_message_first_sleeps_before_close(self):
        """首条消息不是认证消息（认证握手失败）：同样延迟后关闭"""
        server = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        mock_ws = _make_ws([PongMessage().model_dump_json()])

        with patch("tunely.server.asyncio.sleep", new_callable=AsyncMock) as fake_sleep:
            await server._handle_websocket(mock_ws)

        fake_sleep.assert_awaited_once_with(1.0)
        mock_ws.close.assert_awaited_once_with(code=1008)

    @pytest.mark.asyncio
    async def test_disabled_tunnel_no_delay(self):
        """业务拒绝（Tunnel is disabled）不加延迟"""
        server = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        disabled_tunnel = MagicMock()
        disabled_tunnel.enabled = False
        disabled_tunnel.domain = "dom-disabled"
        mock_db, mock_repo = _make_db_repo(get_by_token_return=disabled_tunnel)
        server.db = mock_db

        mock_ws = _make_ws([AuthMessage(token="t-disabled").model_dump_json()])

        with (
            patch("tunely.server.TunnelRepository", return_value=mock_repo),
            patch("tunely.server.asyncio.sleep", new_callable=AsyncMock) as fake_sleep,
        ):
            await server._handle_websocket(mock_ws)

        fake_sleep.assert_not_awaited()
        mock_ws.close.assert_awaited_once_with(code=1008)

    @pytest.mark.asyncio
    async def test_already_connected_no_delay(self):
        """业务拒绝（已有活跃连接）不加延迟"""
        server = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        tunnel = MagicMock()
        tunnel.enabled = True
        tunnel.id = 1
        tunnel.domain = "dom-dup"
        mock_db, mock_repo = _make_db_repo(get_by_token_return=tunnel)
        server.db = mock_db

        mock_ws = _make_ws([AuthMessage(token="t-dup").model_dump_json()])

        server.manager.register = AsyncMock(
            return_value=(False, "已有活跃连接存在，使用 --force 参数可强制抢占")
        )

        with (
            patch("tunely.server.TunnelRepository", return_value=mock_repo),
            patch("tunely.server.asyncio.sleep", new_callable=AsyncMock) as fake_sleep,
        ):
            await server._handle_websocket(mock_ws)

        fake_sleep.assert_not_awaited()
        mock_ws.close.assert_awaited_once_with(code=1008)


# ============== 任务3：每隧道 TCP 并发上限 ==============


def _free_port_sync() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeTunnelConn:
    def __init__(self):
        self.websocket = MagicMock()
        self.websocket.send_text = AsyncMock()


class TestTcpConnectionCap:
    """每隧道 TCP 并发上限：cap=1 时第二条外部连接被拒"""

    def test_count_tcp_connections(self):
        manager = TunnelServer(
            config=TunnelServerConfig()
        ).manager
        assert manager.count_tcp_connections("dom-x") == 0

        writer = MagicMock()
        manager._tcp_connections["c1"] = TcpConnectionState(
            conn_id="c1",
            domain="dom-x",
            reader=MagicMock(),
            writer=writer,
        )
        manager._tcp_connections["c2"] = TcpConnectionState(
            conn_id="c2",
            domain="dom-x",
            reader=MagicMock(),
            writer=MagicMock(),
            closed=True,  # 已关闭不计入
        )
        manager._tcp_connections["c3"] = TcpConnectionState(
            conn_id="c3",
            domain="dom-y",  # 其他隧道不计入
            reader=MagicMock(),
            writer=MagicMock(),
        )
        assert manager.count_tcp_connections("dom-x") == 1
        assert manager.count_tcp_connections("dom-y") == 1

    def test_config_default_and_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("WS_TUNNEL_TCP_MAX_CONNECTIONS", raising=False)
        assert TunnelServerConfig().tcp_max_connections == 0  # 0 = 不限制

        monkeypatch.setenv("WS_TUNNEL_TCP_MAX_CONNECTIONS", "5")
        assert TunnelServerConfig().tcp_max_connections == 5

    @pytest.mark.asyncio
    async def test_second_tcp_connection_rejected_when_cap_reached(self):
        port = _free_port_sync()
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                tcp_listen_host="127.0.0.1",
                tcp_listen=f"{port}:cap-dom",
                tcp_max_connections=1,
            )
        )
        srv._test_port = port
        writer1 = writer2 = None
        try:
            await srv.initialize()
            await srv._create_tunnel(CreateTunnelRequest(domain="cap-dom"), api_key=None)

            ws = _FakeTunnelConn()
            await srv.manager.register(
                websocket=ws.websocket, tunnel_id=1, domain="cap-dom", token="t-cap"
            )

            # 第一条外部连接：正常注册并收到 tcp_connect
            reader1, writer1 = await asyncio.open_connection("127.0.0.1", port)
            for _ in range(100):
                if ws.websocket.send_text.await_count >= 1:
                    break
                await asyncio.sleep(0.02)
            assert ws.websocket.send_text.await_count == 1
            assert srv.manager.count_tcp_connections("cap-dom") == 1

            # 第二条外部连接：达到上限，服务端直接关闭（客户端读到 EOF）
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
            leftover = await asyncio.wait_for(reader2.read(16), timeout=2)
            assert leftover == b""  # EOF

            # 仍只有一条活跃连接，隧道侧没有第二条 tcp_connect
            assert srv.manager.count_tcp_connections("cap-dom") == 1
            assert ws.websocket.send_text.await_count == 1
        finally:
            for w in (writer1, writer2):
                if w:
                    try:
                        w.close()
                    except Exception:
                        pass
            await srv.close()


# ============== 任务4：serve api-key 环境变量回退 ==============


class TestServeApiKeyEnvFallback:
    """--api-key 未显式提供时回退读 WS_TUNNEL_ADMIN_API_KEY"""

    def test_env_fallback_when_flag_missing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WS_TUNNEL_ADMIN_API_KEY", "env-key-123")
        runner = CliRunner()
        with patch("tunely.app.run_app") as mock_run:
            result = runner.invoke(serve, ["--port", "8123"])
            assert result.exit_code == 0, result.output
            assert mock_run.called
            assert mock_run.call_args.kwargs["admin_api_key"] == "env-key-123"

    def test_cli_flag_overrides_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WS_TUNNEL_ADMIN_API_KEY", "env-key-123")
        runner = CliRunner()
        with patch("tunely.app.run_app") as mock_run:
            result = runner.invoke(serve, ["--port", "8123", "--api-key", "cli-key"])
            assert result.exit_code == 0, result.output
            assert mock_run.call_args.kwargs["admin_api_key"] == "cli-key"

    def test_no_env_no_flag_passes_none(self, monkeypatch: pytest.MonkeyPatch):
        runner = CliRunner()
        with patch("tunely.app.run_app") as mock_run:
            result = runner.invoke(serve, ["--port", "8123"])
            assert result.exit_code == 0, result.output
            assert mock_run.call_args.kwargs["admin_api_key"] is None


# ============== 任务5：CORS 默认收紧 ==============


class TestCorsDefaultTightened:
    """CORS 默认空 = 仅同源（不装配 CORSMiddleware）"""

    def test_app_settings_default_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TUNELY_CORS_ORIGINS", raising=False)
        assert AppSettings().cors_origins == ""

    def test_no_cors_middleware_by_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TUNELY_CORS_ORIGINS", raising=False)
        app = create_full_app()
        assert not any(m.cls is CORSMiddleware for m in app.user_middleware)

    def test_empty_config_no_cors_headers_in_response(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from fastapi.testclient import TestClient

        monkeypatch.delenv("TUNELY_CORS_ORIGINS", raising=False)
        client = TestClient(create_full_app())
        resp = client.get("/", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in {
            k.lower() for k in resp.headers
        }

    def test_explicit_origins_still_work(self, monkeypatch: pytest.MonkeyPatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("TUNELY_CORS_ORIGINS", "https://a.example, https://b.example")
        app = create_full_app()
        cors_mw = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        assert cors_mw.kwargs["allow_origins"] == ["https://a.example", "https://b.example"]

        client = TestClient(app)
        resp = client.get("/", headers={"Origin": "https://a.example"})
        assert resp.headers.get("access-control-allow-origin") == "https://a.example"

    def test_wildcard_still_supported(self, monkeypatch: pytest.MonkeyPatch):
        """显式配置 * 时保持旧行为（放行所有来源）"""
        from fastapi.testclient import TestClient

        monkeypatch.setenv("TUNELY_CORS_ORIGINS", "*")
        client = TestClient(create_full_app())
        resp = client.get("/", headers={"Origin": "https://any.example"})
        assert resp.headers.get("access-control-allow-origin") == "*"

    def test_cli_serve_default_empty(self):
        """CLI serve 的 --cors-origins 默认值同步为空"""
        param = next(p for p in serve.params if p.name == "cors_origins")
        assert param.default == ""


# ============== 0.6.1 回归：协议 keepalive 与注销身份校验 ==============


@pytest.mark.asyncio
async def test_unregister_identity_check_prevents_takeover_erasion():
    """force 抢占后，旧连接的退出不得误删新连接的注册（0.6.1 修复）"""
    manager = TunnelManager()
    ws_old, ws_new = MagicMock(), MagicMock()

    await manager.register(websocket=ws_old, tunnel_id=1, domain="d", token="t", force=True)
    await manager.register(websocket=ws_new, tunnel_id=2, domain="d", token="t", force=True)

    # 旧连接退出：注册表仍指向新连接，不应被误删
    await manager.unregister("t", websocket=ws_old)
    conn = manager.get_connection_by_domain("d")
    assert conn is not None and conn.websocket is ws_new

    # 新连接退出：正常注销
    await manager.unregister("t", websocket=ws_new)
    assert manager.get_connection_by_domain("d") is None


def test_ws_ping_gets_pong_full_app():
    """端到端：客户端协议 ping 必须收到 pong（keepalive 依赖此行为，
    服务端消息循环曾缺 Ping 分支导致客户端无限重连——0.6.1 回归）"""
    from fastapi.testclient import TestClient

    from tunely.app import create_full_app

    app = create_full_app(
        domain="ping.test",
        database_url="sqlite+aiosqlite:///:memory:",
        admin_api_key="k",
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/tunnels",
            json={"domain": "ping-dom"},
            headers={"x-api-key": "k"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]

        with client.websocket_connect("/ws/tunnel") as ws:
            ws.send_json({"type": "auth", "token": token, "client_version": "test"})
            auth_ok = ws.receive_json()
            assert auth_ok["type"] == "auth_ok"

            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong", pong
