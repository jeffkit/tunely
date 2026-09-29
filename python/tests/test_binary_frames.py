"""
协议 v2 T2 binary_frames 测试（docs/PROTOCOL_V2.md §1）

- 帧编解码：roundtrip / 布局锚定 / 畸形帧拒绝（版本/类型/长度，F10 语义）
- 服务端：WS 循环 binary 分派——协商连接解帧路由到与 JSON tcp_data 相同的
  落地路径（跨隧道校验 + _route_tcp_payload）；未协商/畸形 binary 丢弃不断连
- 客户端：声明 binary_frames；TcpConnection 双向按协商结果分叉（binary 帧 vs JSON）
"""

import base64
import json
import uuid

import pytest

from tunely.client import TcpConnection, TunnelClient
from tunely.protocol import decode_tcp_data_frame, encode_tcp_data_frame


def _conn_id() -> str:
    return str(uuid.uuid4())


# ============== 帧编解码 ==============


class TestFrameCodec:
    def test_roundtrip(self):
        conn_id = _conn_id()
        payload = b"hello tcp \x00\x01\xff binary"
        frame = encode_tcp_data_frame(conn_id, payload)
        out_conn_id, out_payload = decode_tcp_data_frame(frame)
        assert out_conn_id == conn_id
        assert out_payload == payload

    def test_roundtrip_empty_payload(self):
        conn_id = _conn_id()
        frame = encode_tcp_data_frame(conn_id, b"")
        assert len(frame) == 18
        out_conn_id, out_payload = decode_tcp_data_frame(frame)
        assert out_conn_id == conn_id
        assert out_payload == b""

    def test_layout(self):
        """[0]=0x02 版本标记、[1]=0x01 帧类型、[2:18]=UUID 原始字节、无 sequence"""
        conn_id = _conn_id()
        frame = encode_tcp_data_frame(conn_id, b"abc")
        assert frame[0] == 0x02
        assert frame[1] == 0x01
        assert frame[2:18] == uuid.UUID(conn_id).bytes
        assert frame[18:] == b"abc"
        assert len(frame) == 21

    def test_decode_returns_canonical_lowercase_string(self):
        upper = str(_conn_id()).upper()
        frame = encode_tcp_data_frame(upper, b"x")
        conn_id, payload = decode_tcp_data_frame(frame)
        assert conn_id == upper.lower()
        assert payload == b"x"

    def test_decode_rejects_short_frame(self):
        with pytest.raises(ValueError):
            decode_tcp_data_frame(b"")
        with pytest.raises(ValueError):
            decode_tcp_data_frame(b"\x02\x01\x00\x01")
        with pytest.raises(ValueError):
            decode_tcp_data_frame(b"\x02\x01" + b"\x00" * 15)  # 17 字节

    def test_decode_rejects_bad_version(self):
        frame = bytearray(encode_tcp_data_frame(_conn_id(), b"x"))
        frame[0] = 0x01
        with pytest.raises(ValueError, match="version"):
            decode_tcp_data_frame(bytes(frame))

    def test_decode_rejects_bad_type(self):
        """仅 tcp_data=0x01 一个帧类型"""
        frame = bytearray(encode_tcp_data_frame(_conn_id(), b"x"))
        frame[1] = 0x02
        with pytest.raises(ValueError, match="type"):
            decode_tcp_data_frame(bytes(frame))

    def test_decode_rejects_non_bytes(self):
        with pytest.raises(ValueError):
            decode_tcp_data_frame("not bytes")  # type: ignore[arg-type]

    def test_encode_rejects_invalid_conn_id(self):
        with pytest.raises(ValueError):
            encode_tcp_data_frame("not-a-uuid", b"x")

    def test_large_payload_roundtrip(self):
        conn_id = _conn_id()
        payload = bytes(i % 256 for i in range(65536))
        out_conn_id, out_payload = decode_tcp_data_frame(
            encode_tcp_data_frame(conn_id, payload)
        )
        assert out_conn_id == conn_id
        assert out_payload == payload


# ============== 服务端：WS 循环 binary 分派 ==============


class TestServerBinaryFrames:
    def _create_app(self, **kwargs):
        from tunely.app import create_full_app

        return create_full_app(
            domain="bin.test",
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

    def test_negotiated_binary_full_duplex_over_real_tcp(self):
        """端到端（服务端监听场景，全双工）：

        - 外部 TCP → 客户端：_tcp_read_loop 协商后发 binary 帧（无 JSON/base64）
        - 客户端 → 外部 TCP：binary 帧解出后经 _route_tcp_payload 写入真实 TCP
        """
        import socket

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tunely.app import create_lifespan
        from tunely.config import TunnelServerConfig
        from tunely.server import TunnelServer

        # 占一个空闲端口（关掉后交给监听器，端口复用竞争窗口可接受）
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        domain = "bin-tcp"
        server = TunnelServer(
            config=TunnelServerConfig(
                domain="bin.test",
                database_url="sqlite+aiosqlite:///:memory:",
                admin_api_key="k",
                tcp_listen=f"{port}:{domain}",
            )
        )
        app = FastAPI(lifespan=create_lifespan(server))
        app.include_router(server.router)

        with TestClient(app) as client:
            token = self._create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json(
                    {
                        "type": "auth",
                        "token": token,
                        "capabilities": ["binary_frames"],
                    }
                )
                assert ws.receive_json()["capabilities"] == ["binary_frames"]

                with socket.create_connection(("127.0.0.1", port), timeout=5) as ext:
                    connect_msg = ws.receive_json()
                    assert connect_msg["type"] == "tcp_connect"
                    conn_id = connect_msg["conn_id"]

                    # 外部 → 客户端方向：必须是 binary 帧
                    ext.sendall(b"from-external\x00\xff")
                    frame = ws.receive_bytes()
                    out_conn_id, payload = decode_tcp_data_frame(frame)
                    assert out_conn_id == conn_id
                    assert payload == b"from-external\x00\xff"

                    # 客户端 → 外部方向：binary 帧写入真实 TCP
                    ws.send_bytes(encode_tcp_data_frame(conn_id, b"from-client"))
                    ext.settimeout(5)
                    received = b""
                    while len(received) < len(b"from-client"):
                        chunk = ext.recv(4096)
                        if not chunk:
                            break
                        received += chunk
                    assert received == b"from-client"

    def test_unnegotiated_read_loop_falls_back_to_json(self):
        """未协商（0.7.x 客户端）：_tcp_read_loop 仍发 JSON tcp_data（wire 不变）"""
        import socket

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tunely.app import create_lifespan
        from tunely.config import TunnelServerConfig
        from tunely.server import TunnelServer

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        domain = "bin-tcp-json"
        server = TunnelServer(
            config=TunnelServerConfig(
                domain="bin.test",
                database_url="sqlite+aiosqlite:///:memory:",
                admin_api_key="k",
                tcp_listen=f"{port}:{domain}",
            )
        )
        app = FastAPI(lifespan=create_lifespan(server))
        app.include_router(server.router)

        with TestClient(app) as client:
            token = self._create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json({"type": "auth", "token": token})
                assert ws.receive_json()["capabilities"] == []

                with socket.create_connection(("127.0.0.1", port), timeout=5) as ext:
                    connect_msg = ws.receive_json()
                    assert connect_msg["type"] == "tcp_connect"

                    ext.sendall(b"json-wire")
                    data_msg = ws.receive_json()
                    assert data_msg["type"] == "tcp_data"
                    assert data_msg["conn_id"] == connect_msg["conn_id"]
                    assert base64.b64decode(data_msg["data"]) == b"json-wire"

    def test_unnegotiated_binary_message_dropped_connection_alive(self):
        """未协商收到 binary → 丢弃 + warning，连接不断（后续 ping/pong 正常）"""
        from fastapi.testclient import TestClient
        import tunely.app as app_module

        app = self._create_app()
        with TestClient(app) as client:
            server = app_module.tunnel_server
            assert server is not None
            token = self._create_tunnel(client, "bin-plain")
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json({"type": "auth", "token": token})
                auth_ok = ws.receive_json()
                assert auth_ok["capabilities"] == []

                # 未协商连接发合法 binary 帧 → 丢弃
                ws.send_bytes(encode_tcp_data_frame(_conn_id(), b"junk"))
                # 连接仍健康：ping → pong
                ws.send_json({"type": "ping"})
                pong = ws.receive_json()
                assert pong["type"] == "pong"

    def test_negotiated_malformed_binary_frame_dropped(self):
        """协商连接收到畸形 binary 帧（错版本/错类型/过短）→ 丢弃不断连"""
        from fastapi.testclient import TestClient

        app = self._create_app()
        with TestClient(app) as client:
            token = self._create_tunnel(client, "bin-malformed")
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json(
                    {
                        "type": "auth",
                        "token": token,
                        "capabilities": ["binary_frames"],
                    }
                )
                assert ws.receive_json()["capabilities"] == ["binary_frames"]

                good = bytearray(encode_tcp_data_frame(_conn_id(), b"x"))
                bad_version = bytearray(good)
                bad_version[0] = 0x7F
                bad_type = bytearray(good)
                bad_type[1] = 0x09
                for bad in (bad_version, bad_type, b"\x02\x01\x00"):
                    ws.send_bytes(bytes(bad))

                ws.send_json({"type": "ping"})
                assert ws.receive_json()["type"] == "pong"

    def test_binary_frame_unknown_conn_id_dropped_connection_alive(self):
        """协商连接发无人认领的 binary 帧 → 路由失败仅告警，连接不断"""
        from fastapi.testclient import TestClient

        app = self._create_app()
        with TestClient(app) as client:
            token = self._create_tunnel(client, "bin-orphan")
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json(
                    {
                        "type": "auth",
                        "token": token,
                        "capabilities": ["binary_frames"],
                    }
                )
                ws.receive_json()
                ws.send_bytes(encode_tcp_data_frame(_conn_id(), b"orphan"))
                ws.send_json({"type": "ping"})
                assert ws.receive_json()["type"] == "pong"


# ============== 客户端：声明与双向分叉 ==============


class _FakeWebSocket:
    """记录 send 调用的假 WS（data: str=JSON / bytes=binary 帧）"""

    def __init__(self):
        self.sent: list = []

    async def send(self, data) -> None:
        self.sent.append(data)


class TestClientBinaryFrames:
    def test_client_declares_binary_frames(self):
        """客户端声明已实现的能力：binary_frames（T2）必须在声明集中"""
        caps = TunnelClient._client_capabilities()
        assert "binary_frames" in caps

    @pytest.mark.asyncio
    async def test_tcp_connection_sends_binary_frame_when_negotiated(self):
        ws = _FakeWebSocket()
        conn_id = _conn_id()
        conn = TcpConnection(conn_id, "localhost", 9, ws, binary_frames=True)
        await conn._send_data(b"payload-bytes")

        assert len(ws.sent) == 1
        assert isinstance(ws.sent[0], bytes)
        out_conn_id, payload = decode_tcp_data_frame(ws.sent[0])
        assert out_conn_id == conn_id
        assert payload == b"payload-bytes"

    @pytest.mark.asyncio
    async def test_tcp_connection_sends_json_when_not_negotiated(self):
        """未协商（0.7.x 服务端）时 wire 与旧版逐字节同形：JSON+base64"""
        ws = _FakeWebSocket()
        conn_id = _conn_id()
        conn = TcpConnection(conn_id, "localhost", 9, ws, binary_frames=False)
        await conn._send_data(b"payload-bytes")

        assert len(ws.sent) == 1
        assert isinstance(ws.sent[0], str)
        msg = json.loads(ws.sent[0])
        assert msg["type"] == "tcp_data"
        assert msg["conn_id"] == conn_id
        assert base64.b64decode(msg["data"]) == b"payload-bytes"
        assert "sequence" in msg

    @pytest.mark.asyncio
    async def test_client_route_tcp_payload_writes_local_connection(self):
        """_route_tcp_payload：两路共用的落地写入（未知连接丢弃）"""
        client = TunnelClient(
            server_url="ws://localhost/ws/tunnel",
            token="t",
            target_url="http://localhost:9",
        )

        writes: list[bytes] = []

        class _FakeConn:
            async def write_data(self, data: bytes) -> None:
                writes.append(data)

        fake = _FakeConn()
        client._tcp_connections["known"] = fake  # type: ignore[assignment]

        await client._route_tcp_payload("known", b"abc")
        assert writes == [b"abc"]

        # 未知连接：丢弃不抛
        await client._route_tcp_payload(_conn_id(), b"orphan")
        assert writes == [b"abc"]
