"""
服务端增强特性测试（0.5.0）

- 管理面鉴权：配置 admin api-key 后 POST /api/tunnels 强制鉴权（0.4.x 的洞）
- 多 TCP 监听器：每端口固定绑定一条隧道
- 每隧道流量字节数统计（bytes_in / bytes_out）
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from tunely.config import TunnelServerConfig
from tunely.protocol import TcpDataMessage
from tunely.server import CreateTunnelRequest, TunnelServer


# ============== 管理面鉴权 ==============


@pytest.fixture
def authed_server():
    srv = TunnelServer(
        config=TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            admin_api_key="secret-key",
        )
    )
    return srv


@pytest.mark.asyncio
async def test_create_tunnel_requires_admin_key_when_configured(
    authed_server: TunnelServer,
):
    await authed_server.initialize()

    with pytest.raises(HTTPException) as exc_info:
        await authed_server._create_tunnel(
            CreateTunnelRequest(domain="need-auth"), api_key=None
        )
    assert exc_info.value.status_code == 401

    # 正确 key 可创建
    resp = await authed_server._create_tunnel(
        CreateTunnelRequest(domain="need-auth"), api_key="secret-key"
    )
    assert resp.domain == "need-auth"


@pytest.mark.asyncio
async def test_create_tunnel_without_configured_key_stays_open():
    """未配置 admin api-key 的部署保持内网模式语义（显式选择，非默认）"""
    srv = TunnelServer(
        config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
    )
    await srv.initialize()

    resp = await srv._create_tunnel(CreateTunnelRequest(domain="open-mode"), api_key=None)
    assert resp.domain == "open-mode"


# ============== 多 TCP 监听器 ==============


def _free_port_sync() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeTunnelConn:
    def __init__(self):
        self.websocket = MagicMock()
        self.websocket.send_text = AsyncMock()


def _parse_send(call_args) -> dict:
    return json.loads(call_args.args[0])


@pytest.fixture
def two_port_server():
    port_a = _free_port_sync()
    port_b = _free_port_sync()
    srv = TunnelServer(
        config=TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            tcp_listen_host="127.0.0.1",
            tcp_listen=f"{port_a}:dom-a,{port_b}:dom-b",
            admin_api_key="k",
        )
    )
    srv._test_ports = {"a": port_a, "b": port_b}
    return srv


@pytest.mark.asyncio
async def test_listen_specs_parsing_and_legacy_compat():
    srv = TunnelServer(
        config=TunnelServerConfig(
            tcp_listen="9100: alpha , 9101:beta",
            tcp_listen_port=9100,
            tcp_listen_host="127.0.0.1",
            tcp_target_domain="legacy-dom",
        )
    )
    specs = srv._resolve_tcp_listen_specs()
    # 同端口时 tcp_listen 优先；旧字段在未占用端口上仍生效
    by_port = {p: d for p, _, d in specs}
    assert by_port[9100] == "alpha"
    assert by_port[9101] == "beta"

    # 纯旧字段路径
    srv2 = TunnelServer(
        config=TunnelServerConfig(
            tcp_listen_port=9200, tcp_listen_host="127.0.0.1", tcp_target_domain="old"
        )
    )
    assert srv2._resolve_tcp_listen_specs() == [(9200, "127.0.0.1", "old")]

    # 无任何配置 → 空列表
    srv3 = TunnelServer(config=TunnelServerConfig())
    assert srv3._resolve_tcp_listen_specs() == []


@pytest.mark.asyncio
async def test_multi_listener_routes_by_port(two_port_server: TunnelServer):
    srv = two_port_server
    await srv.initialize()
    try:
        # 先在 DB 注册隧道记录（_get_tunnel 走数据库）
        await srv._create_tunnel(CreateTunnelRequest(domain="dom-a"), api_key="k")
        await srv._create_tunnel(CreateTunnelRequest(domain="dom-b"), api_key="k")

        ws_a = _FakeTunnelConn()
        ws_b = _FakeTunnelConn()
        await srv.manager.register(
            websocket=ws_a.websocket, tunnel_id=1, domain="dom-a", token="t-a"
        )
        await srv.manager.register(
            websocket=ws_b.websocket, tunnel_id=2, domain="dom-b", token="t-b"
        )

        reader_a, writer_a = await asyncio.open_connection("127.0.0.1", srv._test_ports["a"])
        reader_b, writer_b = await asyncio.open_connection("127.0.0.1", srv._test_ports["b"])

        # 等待两个连接的 tcp_connect 均已发出
        for _ in range(100):
            if ws_a.websocket.send_text.await_count >= 1 and ws_b.websocket.send_text.await_count >= 1:
                break
            await asyncio.sleep(0.02)

        # 端口 A 的连接必须路由到 dom-a，端口 B 到 dom-b
        connect_a = _parse_send(ws_a.websocket.send_text.await_args_list[0])
        connect_b = _parse_send(ws_b.websocket.send_text.await_args_list[0])
        assert connect_a["type"] == "tcp_connect"
        assert connect_b["type"] == "tcp_connect"

        # 从端口 A 的外部连接写数据 → 只有 dom-a 收到 tcp_data
        writer_a.write(b"hello-a")
        await writer_a.drain()
        for _ in range(100):
            if ws_a.websocket.send_text.await_count >= 2:
                break
            await asyncio.sleep(0.02)
        data_msg = _parse_send(ws_a.websocket.send_text.await_args_list[1])
        assert data_msg["type"] == "tcp_data"
        assert ws_b.websocket.send_text.await_count == 1  # dom-b 仍只有 connect

        import base64

        conn_id_a = connect_a["conn_id"]
        # 模拟客户端写回数据 → 写入端口 A 的外部连接
        await srv._handle_tcp_data_from_client(
            TcpDataMessage(
                conn_id=conn_id_a,
                data=base64.b64encode(b"reply-a").decode("ascii"),
            )
        )
        reply = await asyncio.wait_for(reader_a.read(64), timeout=2)
        assert reply == b"reply-a"

        # 流量统计：bytes_out = 外部→客户端方向的 hello-a，bytes_in = 客户端→外部 reply-a
        info = await srv._get_tunnel("dom-a", api_key="k")
        assert info.bytes_out >= len(b"hello-a")
        assert info.bytes_in >= len(b"reply-a")
        info_b = await srv._get_tunnel("dom-b", api_key="k")
        assert info_b.bytes_in == 0 and info_b.bytes_out == 0

        writer_a.close()
        writer_b.close()
    finally:
        try:
            writer_a.close()
            writer_b.close()
        except Exception:
            pass
        await srv.close()


@pytest.mark.asyncio
async def test_listeners_closed_on_server_close(two_port_server: TunnelServer):
    srv = two_port_server
    await srv.initialize()
    assert len(srv._tcp_servers) == 2
    ports = {p for p, _, _ in srv._resolve_tcp_listen_specs()}
    assert set(srv._listener_domains) == ports
    for port, _, domain in srv._resolve_tcp_listen_specs():
        assert srv._listener_domains[port] == domain
    await srv.close()
    assert srv._tcp_servers == []
