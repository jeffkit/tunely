"""
安全审计 P0 修复批次测试（0.6.2）

- 1. 路径校验（@-SSRF）：forward 拒绝非 "/" 开头的 path；/t/ 路由归一化补前缀
- 2. 跨隧道消息归属校验：非本隧道的 TunnelResponse / Stream* / Tcp* 被丢弃
- 3. TCP 出口安全默认值（127.0.0.1）+ 空闲超时（tcp_idle_timeout）回收慢连接
- 4. 吊销即断连：delete / regenerate-token 后关闭存量连接
- 5. forward 限额：pending 上限 503 + forward_max_timeout clamp
"""

import asyncio
import json
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import tunely.app
from tunely.app import create_full_app
from tunely.config import TunnelServerConfig
from tunely.protocol import (
    AuthMessage,
    StreamStartMessage,
    TcpCloseMessage,
    TunnelResponse,
)
from tunely.server import (
    ActiveConnection,
    CreateTunnelRequest,
    ForwardResponse,
    TunnelServer,
    is_valid_forward_path,
    normalize_forward_path,
)


# ============== 测试辅助 ==============


def _free_port_sync() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _register_conn(server: TunnelServer, domain: str, token: str | None = None, ws=None):
    """在 manager 里直接注册一个 mock 客户端连接（挂上转发面）"""
    token = token or f"tok-{domain}"
    if ws is None:
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
    conn = ActiveConnection(websocket=ws, tunnel_id=1, domain=domain, token=token)
    server.manager._connections[token] = conn
    server.manager._domain_token_map[domain] = token
    return conn


def _make_client_responder(server: TunnelServer, status: int = 200):
    """mock 一个收到 TunnelRequest 后立即回 TunnelResponse 的隧道客户端"""
    async def respond(text: str) -> None:
        req = json.loads(text)
        await server.manager.complete_request(
            req["id"],
            TunnelResponse(id=req["id"], status=status, headers={}, body="ok"),
        )

    ws = MagicMock()
    ws.send_text = AsyncMock(side_effect=respond)
    ws.close = AsyncMock()
    return ws


def _make_ws_session(receive_texts: list[str]) -> AsyncMock:
    mock_ws = AsyncMock()
    mock_ws.accept = AsyncMock()
    mock_ws.send_text = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.receive_text = AsyncMock(side_effect=receive_texts)
    return mock_ws


def _make_db_repo_domains(domains_by_token: dict[str, str]):
    """mock DB：按 token 返回对应 domain 的隧道行"""
    mock_repo = AsyncMock()
    mock_repo.update_last_connected = AsyncMock()

    async def get_by_token(tok: str):
        dom = domains_by_token.get(tok)
        if dom is None:
            return None
        tunnel = MagicMock()
        tunnel.id = 1
        tunnel.enabled = True
        tunnel.domain = dom
        return tunnel

    mock_repo.get_by_token = AsyncMock(side_effect=get_by_token)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_db = MagicMock()
    mock_db.session = MagicMock(return_value=mock_session)
    return mock_db, mock_repo


# ============== 1. 路径校验（@-SSRF） ==============


class TestForwardPathValidation:
    def test_is_valid_forward_path_unit(self):
        assert not is_valid_forward_path("@169.254.169.254/x")
        assert not is_valid_forward_path("@evil")
        assert not is_valid_forward_path("")
        assert is_valid_forward_path("/")
        assert is_valid_forward_path("/ok")
        assert is_valid_forward_path("/api/chat?x=1")

    def test_normalize_forward_path_unit(self):
        assert normalize_forward_path("@evil/") == "/@evil/"
        assert normalize_forward_path("") == "/"
        assert normalize_forward_path("/ok") == "/ok"

    @pytest.mark.asyncio
    async def test_forward_rejects_at_injection_path(self):
        """攻击样本：path 携带 @host 让客户端改写 host -> 400"""
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        resp = await srv.forward(domain="any", path="@169.254.169.254/x")
        assert resp.status == 400
        assert "Invalid path" in (resp.error or "")

    @pytest.mark.asyncio
    async def test_forward_rejects_relative_path(self):
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        resp = await srv.forward(domain="any", path="ok")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_forward_allows_normal_path(self):
        """/ok 正常放行并原样送达客户端"""
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        ws = _make_client_responder(srv)
        _register_conn(srv, "fwd-dom", ws=ws)

        resp = await srv.forward(domain="fwd-dom", method="GET", path="/ok")
        assert resp.status == 200
        sent = json.loads(ws.send_text.await_args.args[0])
        assert sent["path"] == "/ok"

    def test_forward_endpoint_returns_400_for_at_path(self):
        """HTTP 端点：非法 path 直接 400；合法 path 走既有 503（未连接）语义"""
        app = create_full_app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/tunnels/some-dom/forward",
                json={"path": "@169.254.169.254/x"},
            )
            assert resp.status_code == 400

            ok = client.post("/api/tunnels/some-dom/forward", json={"path": "/ok"})
            assert ok.status_code == 200
            assert ok.json()["status"] == 503  # 未连接的隧道

    def test_t_route_normalizes_path(self):
        """/t/ 浏览器路由：相对/空 path 归一化补 "/" 前缀后转发"""
        app = create_full_app()
        with TestClient(app) as client:
            ts = tunely.app.tunnel_server
            assert ts is not None
            _register_conn(ts, "norm-dom")

            with patch.object(ts, "forward", new_callable=AsyncMock) as fwd:
                fwd.return_value = ForwardResponse(status=200, body="ok")
                resp = client.get("/t/norm-dom/@169.254.169.254/steal")
                assert resp.status_code == 200
                assert fwd.await_args.kwargs["path"] == "/@169.254.169.254/steal"

                fwd.reset_mock()
                client.get("/t/norm-dom")
                assert fwd.await_args.kwargs["path"] == "/"


# ============== 2. 跨隧道消息归属校验 ==============


class TestCrossTunnelMessageIsolation:
    @pytest.fixture
    def server(self) -> TunnelServer:
        return TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )

    async def _run_conn(
        self,
        server: TunnelServer,
        token: str,
        domain: str,
        extra_messages: list,
    ):
        """以真实 register 走一遍 _handle_websocket：auth -> extra 消息 -> 断开"""
        mock_db, mock_repo = _make_db_repo_domains({token: domain})
        server.db = mock_db

        texts = [AuthMessage(token=token).model_dump_json()]
        texts += [m.model_dump_json() for m in extra_messages]

        from fastapi import WebSocketDisconnect

        ws = _make_ws_session(texts + [WebSocketDisconnect()])

        with patch("tunely.server.TunnelRepository", return_value=mock_repo):
            await server._handle_websocket(ws)
        return ws

    @pytest.mark.asyncio
    async def test_tunnel_response_from_other_tunnel_rejected(self, server):
        """dom-a 的连接发来 dom-b pending 的 TunnelResponse -> 不被完成也不被消费"""
        stolen_id = "req-victim"
        future = await server.manager.create_pending_request(stolen_id, domain="dom-b")

        await self._run_conn(
            server, "tok-a", "dom-a", [TunnelResponse(id=stolen_id, status=200)]
        )

        assert not future.done()
        assert server.manager.get_pending_request_domain(stolen_id) == "dom-b"

    @pytest.mark.asyncio
    async def test_tunnel_response_from_own_tunnel_completes(self, server):
        """对照组：本隧道连接回自己的响应 -> 正常完成"""
        rid = "req-own"
        future = await server.manager.create_pending_request(rid, domain="dom-a")

        await self._run_conn(server, "tok-a", "dom-a", [TunnelResponse(id=rid, status=200)])

        assert future.done()
        assert future.result().status == 200

    @pytest.mark.asyncio
    async def test_stream_start_from_other_tunnel_rejected(self, server):
        rid = "stream-victim"
        pending = await server.manager.create_stream_request(rid, domain="dom-b")

        await self._run_conn(
            server, "tok-a", "dom-a", [StreamStartMessage(id=rid, status=200)]
        )

        assert not pending.started
        assert server.manager.get_pending_stream_domain(rid) == "dom-b"

    @pytest.mark.asyncio
    async def test_tcp_close_from_other_tunnel_rejected(self, server):
        """dom-a 掐不掉 dom-b 的 TCP 流"""
        conn_id = "conn-victim"
        future = await server.manager.create_pending_tcp_request(conn_id, domain="dom-b")

        await self._run_conn(server, "tok-a", "dom-a", [TcpCloseMessage(conn_id=conn_id)])

        assert not future.done()
        assert server.manager.get_tcp_owner_domain(conn_id) == "dom-b"

    @pytest.mark.asyncio
    async def test_tcp_close_from_own_tunnel_completes(self, server):
        conn_id = "conn-own"
        future = await server.manager.create_pending_tcp_request(conn_id, domain="dom-a")

        await self._run_conn(server, "tok-a", "dom-a", [TcpCloseMessage(conn_id=conn_id)])

        assert future.done()


# ============== 3. TCP 出口安全默认值 + 空闲超时 ==============


class TestTcpListenDefaultsAndIdleTimeout:
    def test_listen_host_default_loopback(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("WS_TUNNEL_TCP_LISTEN_HOST", raising=False)
        assert TunnelServerConfig().tcp_listen_host == "127.0.0.1"

    def test_listen_host_env_override(self, monkeypatch: pytest.MonkeyPatch):
        """容器/公网部署显式放开 0.0.0.0 仍然有效"""
        monkeypatch.setenv("WS_TUNNEL_TCP_LISTEN_HOST", "0.0.0.0")
        assert TunnelServerConfig().tcp_listen_host == "0.0.0.0"

    def test_idle_timeout_default_and_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("WS_TUNNEL_TCP_IDLE_TIMEOUT", raising=False)
        assert TunnelServerConfig().tcp_idle_timeout == 300

        monkeypatch.setenv("WS_TUNNEL_TCP_IDLE_TIMEOUT", "1")
        assert TunnelServerConfig().tcp_idle_timeout == 1

    @pytest.mark.asyncio
    async def test_idle_timeout_closes_slow_connection(self):
        """连上不发数据：idle_timeout=1s 后服务端主动关闭（客户端读到 EOF）"""
        port = _free_port_sync()
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                tcp_listen=f"{port}:idle-dom",
                tcp_idle_timeout=1,
            )
        )
        writer = None
        try:
            await srv.initialize()

            ws = MagicMock()
            ws.send_text = AsyncMock()
            await srv.manager.register(
                websocket=ws, tunnel_id=1, domain="idle-dom", token="tok-idle"
            )

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            for _ in range(100):
                if ws.send_text.await_count >= 1:  # 收到 TcpConnectMessage
                    break
                await asyncio.sleep(0.02)

            start = asyncio.get_event_loop().time()
            leftover = await asyncio.wait_for(reader.read(16), timeout=3)
            elapsed = asyncio.get_event_loop().time() - start

            assert leftover == b""  # EOF：服务端关闭了连接
            assert elapsed < 2.5  # 远小于默认 300s，说明空闲超时生效
        finally:
            if writer:
                writer.close()
            await srv.close()


# ============== 4. 吊销即断连 ==============


class TestRevocationClosesConnection:
    def _make_server(self) -> TunnelServer:
        return TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                admin_api_key="k",
            )
        )

    @pytest.mark.asyncio
    async def test_delete_tunnel_closes_active_connection(self):
        srv = self._make_server()
        await srv.initialize()
        try:
            await srv._create_tunnel(CreateTunnelRequest(domain="del-conn"), api_key="k")

            ws = MagicMock()
            ws.send_text = AsyncMock()
            ws.close = AsyncMock()
            _register_conn(srv, "del-conn", token="tok-del", ws=ws)

            result = await srv._delete_tunnel("del-conn", api_key="k")
            assert result["success"] is True

            ws.close.assert_awaited_once_with(code=1000, reason="tunnel revoked")

            # 审计带 revoked 标记
            logs = await srv._get_audit_logs(10, api_key="k")
            del_log = next(l for l in logs if l["action"] == "delete")
            assert "revoked" in (del_log["detail"] or "")
        finally:
            await srv.close()

    @pytest.mark.asyncio
    async def test_regenerate_token_closes_active_connection(self):
        srv = self._make_server()
        await srv.initialize()
        try:
            await srv._create_tunnel(CreateTunnelRequest(domain="rot-conn"), api_key="k")

            ws = MagicMock()
            ws.send_text = AsyncMock()
            ws.close = AsyncMock()
            _register_conn(srv, "rot-conn", token="tok-rot", ws=ws)

            await srv._regenerate_token("rot-conn", api_key="k")

            ws.close.assert_awaited_once_with(code=1000, reason="token rotated")

            logs = await srv._get_audit_logs(10, api_key="k")
            regen_log = next(l for l in logs if l["action"] == "regenerate")
            assert "rotated" in (regen_log["detail"] or "")
        finally:
            await srv.close()

    @pytest.mark.asyncio
    async def test_delete_without_connection_still_succeeds(self):
        """没有存量连接时吊销照常成功（不 close、不报错）"""
        srv = self._make_server()
        await srv.initialize()
        try:
            await srv._create_tunnel(CreateTunnelRequest(domain="del-noconn"), api_key="k")
            result = await srv._delete_tunnel("del-noconn", api_key="k")
            assert result["success"] is True
        finally:
            await srv.close()


# ============== 5. forward 限额 ==============


class TestForwardLimits:
    def test_forward_max_timeout_default_and_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("WS_TUNNEL_FORWARD_MAX_TIMEOUT", raising=False)
        assert TunnelServerConfig().forward_max_timeout == 600.0

        monkeypatch.setenv("WS_TUNNEL_FORWARD_MAX_TIMEOUT", "30")
        assert TunnelServerConfig().forward_max_timeout == 30.0

    @pytest.mark.asyncio
    async def test_forward_503_when_pending_full(self):
        """pending 达到 max_pending_requests 上限后返回 503，且不新增 pending"""
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                max_pending_requests=2,
            )
        )
        _register_conn(srv, "cap-dom")
        await srv.manager.create_pending_request("r1", domain="cap-dom")
        await srv.manager.create_pending_request("r2", domain="cap-dom")

        resp = await srv.forward(domain="cap-dom", path="/x")
        assert resp.status == 503
        assert "Too many pending" in (resp.error or "")
        assert srv.manager.pending_requests_count() == 2

    @pytest.mark.asyncio
    async def test_forward_timeout_clamped_to_limit(self):
        """超长 timeout 被 clamp 到 forward_max_timeout（600）"""
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                forward_max_timeout=600,
            )
        )
        ws = _make_client_responder(srv)
        _register_conn(srv, "clamp-dom", ws=ws)

        resp = await srv.forward(domain="clamp-dom", path="/x", timeout=10000)
        assert resp.status == 200
        sent = json.loads(ws.send_text.await_args.args[0])
        assert sent["timeout"] == 600

    @pytest.mark.asyncio
    async def test_forward_timeout_below_cap_untouched(self):
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                forward_max_timeout=600,
            )
        )
        ws = _make_client_responder(srv)
        _register_conn(srv, "below-dom", ws=ws)

        resp = await srv.forward(domain="below-dom", path="/x", timeout=5)
        assert resp.status == 200
        sent = json.loads(ws.send_text.await_args.args[0])
        assert sent["timeout"] == 5

    @pytest.mark.asyncio
    async def test_forward_timeout_unlimited_when_zero(self):
        """forward_max_timeout=0 表示不限制（部署显式关闭 clamp）"""
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                forward_max_timeout=0,
            )
        )
        ws = _make_client_responder(srv)
        _register_conn(srv, "nocap-dom", ws=ws)

        resp = await srv.forward(domain="nocap-dom", path="/x", timeout=10000)
        assert resp.status == 200
        sent = json.loads(ws.send_text.await_args.args[0])
        assert sent["timeout"] == 10000
