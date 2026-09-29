"""
0.8.0 协议 v2 能力协商测试（docs/PROTOCOL_V2.md §0）

- AuthMessage / AuthOkMessage 增加可选 capabilities: list[str]，缺字段 = 空集合
- 协商语义：服务端注册表 ∩ 客户端声明 − kill 开关禁用集，只回交集
- 服务端注册表 SERVER_CAPABILITIES 初始为空（T2/T3 实现后再注册能力名）
- ActiveConnection.capabilities 存协商结果（本任务只存不用）
- 端到端：auth 带 capabilities → auth_ok 回交集；kill 开关经 env 生效
"""

import pytest
from unittest.mock import MagicMock

import tunely.server as server_module
from tunely.config import TunnelServerConfig
from tunely.protocol import AuthMessage, AuthOkMessage
from tunely.server import SERVER_CAPABILITIES, ActiveConnection, TunnelServer


def _make_server(**config_kwargs) -> TunnelServer:
    return TunnelServer(
        config=TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:", **config_kwargs
        )
    )


class TestNegotiateCapabilities:
    def test_registry_has_binary_frames(self):
        """T2 起注册表含 binary_frames（chunked_http 等 T3 实现后再注册）"""
        assert SERVER_CAPABILITIES == ["binary_frames"]

    def test_intersection_semantics(self, monkeypatch):
        """交集语义：客户端声明 ∩ 服务端注册表；声明了未注册能力不启用"""
        monkeypatch.setattr(
            server_module, "SERVER_CAPABILITIES", ["binary_frames", "chunked_http"]
        )
        srv = _make_server()
        assert srv._negotiate_capabilities(["binary_frames", "other"]) == [
            "binary_frames"
        ]

    def test_unregistered_capability_negotiates_nothing(self):
        """未注册进注册表的能力（chunked_http，T3 未实现）声明了也不启用"""
        srv = _make_server()
        assert srv._negotiate_capabilities(["chunked_http"]) == []

    def test_missing_or_nonlist_caps_is_empty_set(self):
        """客户端缺失声明 = 空集合；防御式：非列表输入也按空集合"""
        srv = _make_server()
        assert srv._negotiate_capabilities([]) == []
        assert srv._negotiate_capabilities(None) == []
        assert srv._negotiate_capabilities("binary_frames") == []

    def test_kill_switch_removes_from_registry(self, monkeypatch):
        """kill 开关：disable_capabilities 逗号切分 strip 后从注册表剔除"""
        monkeypatch.setattr(
            server_module, "SERVER_CAPABILITIES", ["binary_frames", "chunked_http"]
        )
        srv = _make_server(disable_capabilities=" binary_frames , nosuch_cap ")
        assert srv._negotiate_capabilities(["binary_frames", "chunked_http"]) == [
            "chunked_http"
        ]

    def test_kill_switch_all(self, monkeypatch):
        """kill 开关禁用全部已声明能力 → 协商结果为空"""
        monkeypatch.setattr(
            server_module, "SERVER_CAPABILITIES", ["binary_frames", "chunked_http"]
        )
        srv = _make_server(disable_capabilities="binary_frames,chunked_http")
        assert srv._negotiate_capabilities(["binary_frames", "chunked_http"]) == []

    def test_result_sorted_stable(self, monkeypatch):
        """协商结果排序稳定（wire 形状确定，便于三端断言）"""
        monkeypatch.setattr(server_module, "SERVER_CAPABILITIES", ["c", "a", "b"])
        srv = _make_server()
        assert srv._negotiate_capabilities(["c", "a", "b"]) == ["a", "b", "c"]


class TestProtocolModelCapabilities:
    def test_auth_message_defaults_empty(self):
        """AuthMessage.capabilities 缺省空列表（旧客户端不带字段照常解析）"""
        msg = AuthMessage(token="t")
        assert msg.capabilities == []

    def test_auth_message_parses_capabilities(self):
        msg = AuthMessage(token="t", capabilities=["binary_frames"])
        assert msg.capabilities == ["binary_frames"]

    def test_auth_ok_missing_field_parses_empty(self):
        """0.7.3 风格 auth_ok 不带 capabilities → 空集合（wire 向后兼容）"""
        msg = AuthOkMessage.model_validate(
            {
                "type": "auth_ok",
                "domain": "d",
                "tunnel_id": "1",
                "server_version": "0.7.3",
            }
        )
        assert msg.capabilities == []

    def test_auth_ok_parses_capabilities(self):
        msg = AuthOkMessage.model_validate(
            {
                "type": "auth_ok",
                "domain": "d",
                "tunnel_id": "1",
                "capabilities": ["binary_frames"],
            }
        )
        assert msg.capabilities == ["binary_frames"]


class TestRegisterStoresCapabilities:
    @pytest.mark.asyncio
    async def test_register_stores_negotiated_capabilities(self):
        """register 存协商结果（后续 T2/T3 按连接门控，本任务只存不用）"""
        srv = _make_server()
        try:
            ok, err = await srv.manager.register(
                MagicMock(),
                1,
                "cap-dom",
                "tok-cap",
                capabilities=frozenset({"binary_frames"}),
            )
            assert ok and err is None
            conn = srv.manager.get_connection_by_token("tok-cap")
            assert conn is not None
            assert conn.capabilities == frozenset({"binary_frames"})
        finally:
            await srv.close()

    @pytest.mark.asyncio
    async def test_register_defaults_empty_frozenset(self):
        """register 不传 capabilities → 空 frozenset（旧调用方零改动）"""
        srv = _make_server()
        try:
            ok, _ = await srv.manager.register(MagicMock(), 2, "cap-dom2", "tok-cap2")
            assert ok
            conn = srv.manager.get_connection_by_token("tok-cap2")
            assert conn is not None
            assert conn.capabilities == frozenset()
        finally:
            await srv.close()


class TestCapabilitiesEndToEnd:
    def _create_app(self, **kwargs):
        from tunely.app import create_full_app

        return create_full_app(
            domain="cap.test",
            database_url="sqlite+aiosqlite:///:memory:",
            admin_api_key="k",
            **kwargs,
        )

    def _create_tunnel(self, client, domain: str) -> str:
        resp = client.post(
            "/api/tunnels", json={"domain": domain}, headers={"x-api-key": "k"}
        )
        assert resp.status_code in (200, 201), resp.text
        return resp.json()["token"]

    def test_auth_negotiates_intersection_over_ws(self, monkeypatch):
        """端到端：auth 带 capabilities → auth_ok 回服务端注册表交集"""
        from fastapi.testclient import TestClient

        monkeypatch.setattr(
            server_module, "SERVER_CAPABILITIES", ["binary_frames", "chunked_http"]
        )
        app = self._create_app()
        with TestClient(app) as client:
            token = self._create_tunnel(client, "cap-e2e")
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json(
                    {
                        "type": "auth",
                        "token": token,
                        "capabilities": ["binary_frames", "other"],
                    }
                )
                auth_ok = ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        assert auth_ok["capabilities"] == ["binary_frames"]

    def test_auth_without_capabilities_returns_empty(self, monkeypatch):
        """端到端：客户端不带 capabilities（0.7.3 形状）→ auth_ok.capabilities == []"""
        from fastapi.testclient import TestClient

        monkeypatch.setattr(server_module, "SERVER_CAPABILITIES", ["binary_frames"])
        app = self._create_app()
        with TestClient(app) as client:
            token = self._create_tunnel(client, "cap-e2e-plain")
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json({"type": "auth", "token": token})
                auth_ok = ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        assert auth_ok["capabilities"] == []

    def test_kill_switch_via_env(self, monkeypatch):
        """端到端：WS_TUNNEL_DISABLE_CAPABILITIES 剔除后 auth_ok 不含该能力"""
        from fastapi.testclient import TestClient

        monkeypatch.setenv("WS_TUNNEL_DISABLE_CAPABILITIES", "binary_frames")
        monkeypatch.setattr(server_module, "SERVER_CAPABILITIES", ["binary_frames"])
        app = self._create_app()
        with TestClient(app) as client:
            token = self._create_tunnel(client, "cap-e2e-kill")
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json(
                    {
                        "type": "auth",
                        "token": token,
                        "capabilities": ["binary_frames"],
                    }
                )
                auth_ok = ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        assert auth_ok["capabilities"] == []
