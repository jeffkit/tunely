"""
0.7.2 真实版本上报测试（ROLLING_UPGRADE.md 第 1 级）

- 注册时记录 AuthMessage.client_version，manager 查询面可取（未连接 None）
- register 缺省 unknown（不再是假值 0.1.0）
- py 客户端上报真实版本（历史不传被默认成假值 0.1.0）
- AuthOk.server_version：默认回空串（探测去特征化已落地，docs/PROBE_HARDENING.md T6），
  WS_TUNNEL_EXPOSE_VERSION=true 才回真实版本；/api/info 不受该开关影响
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

import tunely
from tunely.client import TunnelClient
from tunely.config import TunnelServerConfig
from tunely.server import ActiveConnection, TunnelManager, TunnelServer


def _register_conn(server: TunnelServer, domain: str, client_version: str):
    """在 manager 上注册一条带自报版本的连接（复用 p1 加固测试的辅助形状）"""
    token = f"tok-{domain}"
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    server.manager._connections[token] = ActiveConnection(
        websocket=ws,
        tunnel_id=1,
        domain=domain,
        token=token,
        client_version=client_version,
    )
    server.manager._domain_token_map[domain] = token


class TestServerVersionReporting:
    def test_server_version_matches_package(self):
        """_server_version 返回执行代码的真实版本（不许是 0.1.0 假值/unknown）"""
        v = TunnelServer._server_version()
        assert v == tunely.__version__
        assert v != "0.1.0"

    def test_api_info_reports_same_version(self):
        from fastapi.testclient import TestClient

        from tunely.app import create_full_app

        app = create_full_app(database_url="sqlite+aiosqlite:///:memory:")
        with TestClient(app) as client:
            assert client.get("/api/info").json()["version"] == tunely.__version__

    def test_auth_ok_server_version_default_empty(self):
        """默认不回真实版本（已落地）（docs/PROBE_HARDENING.md T6）：auth_ok.server_version = """""
        from fastapi.testclient import TestClient

        from tunely.app import create_full_app

        app = create_full_app(
            domain="ver.test",
            database_url="sqlite+aiosqlite:///:memory:",
            admin_api_key="k",
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/tunnels", json={"domain": "ver-dom-empty"}, headers={"x-api-key": "k"}
            )
            token = resp.json()["token"]

            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json({"type": "auth", "token": token, "client_version": "0.2.7"})
                auth_ok = ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        assert auth_ok["server_version"] == ""

    def test_auth_ok_carries_real_server_version_when_exposed(self, monkeypatch):
        """WS_TUNNEL_EXPOSE_VERSION=true 时 auth_ok.server_version = 真实版本（历史行为）"""
        from fastapi.testclient import TestClient

        from tunely.app import create_full_app

        monkeypatch.setenv("WS_TUNNEL_EXPOSE_VERSION", "true")
        app = create_full_app(
            domain="ver.test",
            database_url="sqlite+aiosqlite:///:memory:",
            admin_api_key="k",
        )
        with TestClient(app) as client:
            resp = client.post(
                "/api/tunnels", json={"domain": "ver-dom"}, headers={"x-api-key": "k"}
            )
            token = resp.json()["token"]

            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json(
                    {"type": "auth", "token": token, "client_version": "0.2.7"}
                )
                auth_ok = ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        assert auth_ok["server_version"] == tunely.__version__


class TestClientVersionFleet:
    @pytest.mark.asyncio
    async def test_tunnel_info_exposes_client_version(self):
        """manager 查询面：连着的隧道报自报版本，未连接 None"""
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        try:
            _register_conn(srv, "cv-dom", client_version="0.2.7")
            assert srv.manager.get_client_version("cv-dom") == "0.2.7"
            assert srv.manager.get_client_version("not-connected") is None
        finally:
            await srv.close()

    @pytest.mark.asyncio
    async def test_register_defaults_unknown(self):
        """register 不传/传空 client_version 时缺省 unknown（不再是假值 0.1.0）"""
        manager = TunnelManager()
        ok, err = await manager.register(MagicMock(), 1, "d1", "tok-d1")
        assert ok and err is None
        conn = manager.get_connection_by_token("tok-d1")
        assert conn is not None
        assert conn.client_version == "unknown"

        ok, _ = await manager.register(
            MagicMock(), 2, "d2", "tok-d2", client_version=""
        )
        assert ok
        conn2 = manager.get_connection_by_token("tok-d2")
        assert conn2 is not None
        assert conn2.client_version == "unknown"


class TestPyClientVersion:
    def test_client_reports_real_version(self):
        """py 客户端自报真实版本（历史不传字段，被 pydantic 默认成假值 0.1.0）"""
        v = TunnelClient._client_version()
        assert v == tunely.__version__
        assert v != "0.1.0"
