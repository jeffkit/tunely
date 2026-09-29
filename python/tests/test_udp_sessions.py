"""
协议 v2 T4 UDP 会话测试（docs/PROTOCOL_V2.md §5）

- 帧编解码：0x03 帧 roundtrip / 布局锚定 / 已知向量（与 rust 同向量跨语言对齐）/
  畸形帧拒绝（版本/类型/长度，F10 语义）/ 帧类型互斥
- parse_message_fast 新分支：udp_open / udp_close 轻校验直构（畸形抛 ValueError）
- 服务端会话生命周期（真实 UDP socket e2e，手法对齐 test_binary_frames 的真实 TCP e2e）：
  外部首包 → udp_open JSON + 0x03 帧；客户端回帧 → echo 目标收到 → 目标回包 →
  服务端按会话路由回外部 addr；同源后续包不再发 udp_open
- 门控与限额：未协商 udp 丢包不断连（0.8.0 客户端 × 新服务端行为不变）；
  无隧道连接丢包；会话数上限；空闲超时回收；客户端主动 udp_close 删映射；
  WS 断连清会话表；UDP/TCP 同端口号并存
- 客户端：声明 udp；udp_open 建真实 socket 双向打流通；udp_close / 断连清理
"""

import asyncio
import json
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest

from tunely.client import TunnelClient, UdpSession
from tunely.config import TunnelServerConfig
from tunely.protocol import (
    UdpCloseMessage,
    UdpOpenMessage,
    decode_tcp_data_frame,
    decode_udp_data_frame,
    encode_tcp_data_frame,
    encode_udp_data_frame,
    parse_message,
    parse_message_fast,
)


def _session_id() -> str:
    return str(uuid.uuid4())


def _free_udp_port() -> int:
    """占一个空闲 UDP 端口（关掉后交给监听器，端口复用竞争窗口可接受）"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


# ============== 帧编解码 ==============


class TestUdpFrameCodec:
    def test_roundtrip(self):
        session_id = _session_id()
        payload = b"hello udp \x00\x01\xff datagram"
        frame = encode_udp_data_frame(session_id, payload)
        out_session_id, out_payload = decode_udp_data_frame(frame)
        assert out_session_id == session_id
        assert out_payload == payload

    def test_roundtrip_empty_payload(self):
        session_id = _session_id()
        frame = encode_udp_data_frame(session_id, b"")
        assert len(frame) == 18
        out_session_id, out_payload = decode_udp_data_frame(frame)
        assert out_session_id == session_id
        assert out_payload == b""

    def test_layout(self):
        """[0]=0x02 版本标记、[1]=0x03 帧类型、[2:18]=session_id 原始字节、无 sequence"""
        session_id = _session_id()
        frame = encode_udp_data_frame(session_id, b"abc")
        assert frame[0] == 0x02
        assert frame[1] == 0x03
        assert frame[2:18] == uuid.UUID(session_id).bytes
        assert frame[18:] == b"abc"
        assert len(frame) == 21

    def test_known_vector_matches_rust(self):
        """跨语言对齐锚：与 rust/src/protocol.rs udp_frame_known_vector_matches_python 同向量"""
        frame = encode_udp_data_frame("00112233-4455-4677-8899-aabbccddeeff", b"hi")
        expected = (
            bytes([0x02, 0x03])
            + bytes.fromhex("001122334455467788 99aabbccddeeff".replace(" ", ""))
            + b"hi"
        )
        assert frame == expected

    def test_decode_returns_canonical_lowercase_string(self):
        upper = _session_id().upper()
        frame = encode_udp_data_frame(upper, b"x")
        session_id, payload = decode_udp_data_frame(frame)
        assert session_id == upper.lower()
        assert payload == b"x"

    def test_decode_rejects_short_frame(self):
        with pytest.raises(ValueError):
            decode_udp_data_frame(b"")
        with pytest.raises(ValueError):
            decode_udp_data_frame(b"\x02\x03\x00\x01")
        with pytest.raises(ValueError):
            decode_udp_data_frame(b"\x02\x03" + b"\x00" * 15)  # 17 字节

    def test_decode_rejects_bad_version(self):
        frame = bytearray(encode_udp_data_frame(_session_id(), b"x"))
        frame[0] = 0x01
        with pytest.raises(ValueError, match="version"):
            decode_udp_data_frame(bytes(frame))

    def test_decode_rejects_bad_type(self):
        frame = bytearray(encode_udp_data_frame(_session_id(), b"x"))
        frame[1] = 0x09
        with pytest.raises(ValueError, match="type"):
            decode_udp_data_frame(bytes(frame))

    def test_frame_types_are_mutually_exclusive(self):
        """tcp 帧（0x01）不能当 udp 帧解；udp 帧（0x03）不能当 tcp 帧解"""
        sid = _session_id()
        with pytest.raises(ValueError, match="type"):
            decode_udp_data_frame(encode_tcp_data_frame(sid, b"x"))
        with pytest.raises(ValueError, match="type"):
            decode_tcp_data_frame(encode_udp_data_frame(sid, b"x"))

    def test_decode_rejects_non_bytes(self):
        with pytest.raises(ValueError):
            decode_udp_data_frame("not bytes")  # type: ignore[arg-type]

    def test_encode_rejects_invalid_session_id(self):
        with pytest.raises(ValueError):
            encode_udp_data_frame("not-a-uuid", b"x")

    def test_large_payload_roundtrip(self):
        session_id = _session_id()
        payload = bytes(i % 256 for i in range(65536))
        out_session_id, out_payload = decode_udp_data_frame(
            encode_udp_data_frame(session_id, payload)
        )
        assert out_session_id == session_id
        assert out_payload == payload


# ============== parse_message / parse_message_fast 新分支 ==============


class TestParseUdpMessages:
    def test_parse_message_udp_open(self):
        msg = parse_message({"type": "udp_open", "session_id": _session_id()})
        assert isinstance(msg, UdpOpenMessage)
        assert msg.type == "udp_open"

    def test_parse_message_udp_close(self):
        msg = parse_message(
            {"type": "udp_close", "session_id": _session_id(), "reason": "idle timeout"}
        )
        assert isinstance(msg, UdpCloseMessage)
        assert msg.reason == "idle timeout"

    def test_parse_fast_udp_open(self):
        sid = _session_id()
        msg = parse_message_fast(
            json.dumps({"type": "udp_open", "session_id": sid, "timestamp": "now"})
        )
        assert isinstance(msg, UdpOpenMessage)
        assert msg.session_id == sid
        assert msg.timestamp == "now"

    def test_parse_fast_udp_close(self):
        sid = _session_id()
        msg = parse_message_fast(json.dumps({"type": "udp_close", "session_id": sid}))
        assert isinstance(msg, UdpCloseMessage)
        assert msg.session_id == sid
        assert msg.reason is None

        msg = parse_message_fast(
            json.dumps({"type": "udp_close", "session_id": sid, "reason": "socket error"})
        )
        assert msg.reason == "socket error"

    def test_parse_fast_udp_missing_session_id_raises(self):
        for raw in (
            json.dumps({"type": "udp_open"}),
            json.dumps({"type": "udp_close"}),
            json.dumps({"type": "udp_open", "session_id": 123}),
        ):
            with pytest.raises(ValueError):
                parse_message_fast(raw)


# ============== 服务端：真实 UDP socket e2e ==============


class _EchoTarget:
    """测试用 UDP echo 目标（真实 socket + 后台线程回显：收到即原样回发）"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.addr = self.sock.getsockname()
        self._thread = threading.Thread(target=self._echo_loop, daemon=True)
        self._thread.start()

    def _echo_loop(self) -> None:
        self.sock.settimeout(0.2)
        while True:
            try:
                data, sender = self.sock.recvfrom(65536)
                self.sock.sendto(data, sender)
            except socket.timeout:
                continue
            except OSError:
                break  # socket 已关闭

    def close(self) -> None:
        self.sock.close()


def _client_target_roundtrip(echo: _EchoTarget, payload: bytes) -> bytes:
    """模拟隧道客户端的目标侧行为：payload 发往目标，等 echo 回发后返回回包
    （回包即客户端将组 0x03 帧回服务端的数据）"""
    c_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c_sock.settimeout(5)
    try:
        c_sock.sendto(payload, echo.addr)
        data, sender = c_sock.recvfrom(65536)
        assert data == payload, "echo 目标应原样回发"
        assert sender == echo.addr
        return data
    finally:
        c_sock.close()


class TestServerUdpSessions:
    def _make_server(self, domain: str, port: int, **config_kwargs):
        from fastapi import FastAPI

        from tunely.app import create_lifespan
        from tunely.server import TunnelServer

        server = TunnelServer(
            config=TunnelServerConfig(
                domain="udp.test",
                database_url="sqlite+aiosqlite:///:memory:",
                admin_api_key="k",
                udp_listen=f"{port}:{domain}",
                **config_kwargs,
            )
        )
        app = FastAPI(lifespan=create_lifespan(server))
        app.include_router(server.router)
        return server, app

    def _create_tunnel(self, client, domain: str) -> str:
        resp = client.post(
            "/api/tunnels", json={"domain": domain}, headers={"x-api-key": "k"}
        )
        assert resp.status_code in (200, 201), resp.text
        return resp.json()["token"]

    def _auth_ws(self, ws, token: str, capabilities: list[str]) -> dict:
        ws.send_json({"type": "auth", "token": token, "capabilities": capabilities})
        auth_ok = ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        return auth_ok

    def test_config_defaults(self):
        config = TunnelServerConfig()
        assert config.udp_listen is None
        assert config.udp_session_timeout == 60
        assert config.udp_max_sessions == 256

    def test_full_session_lifecycle_and_two_way_datagrams(self):
        """端到端（真实 UDP socket，全双工）：

        外部首包 → 客户端收到 udp_open JSON + 0x03 帧（先 open 后帧，WS 有序）；
        客户端回 0x03 帧 → echo 目标收到；目标回包 → 服务端按会话路由回外部 addr；
        同源后续包直接 0x03 帧，不再重复 udp_open。
        """
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-e2e"
        server, app = self._make_server(domain, port)

        echo = _EchoTarget()
        try:
            with TestClient(app) as client:
                token = self._create_tunnel(client, domain)
                with client.websocket_connect("/ws/tunnel") as ws:
                    auth_ok = self._auth_ws(ws, token, ["binary_frames", "udp"])
                    assert auth_ok["capabilities"] == ["binary_frames", "udp"]

                    ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    ext.settimeout(5)
                    try:
                        # ---- 外部首包：udp_open + 0x03 帧（先 open 后帧）----
                        ext.sendto(b"ping-1\x00\xff", ("127.0.0.1", port))
                        open_msg = ws.receive_json()
                        assert open_msg["type"] == "udp_open"
                        session_id = open_msg["session_id"]
                        assert len(session_id) == 36

                        frame = ws.receive_bytes()
                        out_sid, payload = decode_udp_data_frame(frame)
                        assert out_sid == session_id
                        assert payload == b"ping-1\x00\xff"
                        assert len(server.manager._udp_sessions) == 1

                        # ---- 客户端 → 目标；目标回包 → 客户端 → 服务端 → 外部 addr ----
                        echoed = _client_target_roundtrip(echo, payload)
                        ws.send_bytes(encode_udp_data_frame(session_id, echoed))

                        data, addr = ext.recvfrom(65536)
                        assert data == echoed
                        # 回程来源 = 服务端 UDP 监听端口（按会话 sendto 回原外部 addr）
                        assert addr == ("127.0.0.1", port)

                        # ---- 同源第二包：不再 udp_open，直接帧 ----
                        ext.sendto(b"ping-2", ("127.0.0.1", port))
                        frame2 = ws.receive_bytes()  # 若误发 udp_open 会在此抛错
                        out_sid2, payload2 = decode_udp_data_frame(frame2)
                        assert out_sid2 == session_id
                        assert payload2 == b"ping-2"
                        assert len(server.manager._udp_sessions) == 1

                        echoed2 = _client_target_roundtrip(echo, payload2)
                        ws.send_bytes(encode_udp_data_frame(session_id, echoed2))
                        data2, _ = ext.recvfrom(65536)
                        assert data2 == echoed2
                    finally:
                        ext.close()
        finally:
            echo.close()

    def test_unnegotiated_udp_packet_dropped_connection_alive(self):
        """未协商 udp（0.8.0 客户端 × 新服务端开 UDP 监听）：丢包 + warning，连接不断，
        不产生 udp_open 不建会话（WS 有序：包被丢弃后紧随的 ping 得到 pong）"""
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-plain"
        server, app = self._make_server(domain, port)

        with TestClient(app) as client:
            token = self._create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                auth_ok = self._auth_ws(ws, token, [])
                assert auth_ok["capabilities"] == []

                ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    ext.sendto(b"dropped", ("127.0.0.1", port))
                finally:
                    ext.close()

                ws.send_json({"type": "ping"})
                assert ws.receive_json()["type"] == "pong"
                assert len(server.manager._udp_sessions) == 0

    def test_no_tunnel_connected_packet_dropped_no_session(self):
        """无隧道连接时收到 UDP 包：丢弃（仅 warning），无会话残留；
        之后隧道连上再发包可正常建会话"""
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-late"
        server, app = self._make_server(domain, port)

        ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with TestClient(app):
                ext.sendto(b"early", ("127.0.0.1", port))
                time.sleep(0.3)
                assert len(server.manager._udp_sessions) == 0

            with TestClient(app) as client:
                token = self._create_tunnel(client, domain)
                with client.websocket_connect("/ws/tunnel") as ws:
                    self._auth_ws(ws, token, ["binary_frames", "udp"])
                    ext.sendto(b"real", ("127.0.0.1", port))
                    assert ws.receive_json()["type"] == "udp_open"
        finally:
            ext.close()

    def test_session_limit_drops_new_addrs(self):
        """udp_max_sessions=2：前两个外部 addr 建会话，第三个丢包（连接不断）"""
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-cap"
        server, app = self._make_server(domain, port, udp_max_sessions=2)

        with TestClient(app) as client:
            token = self._create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                self._auth_ws(ws, token, ["binary_frames", "udp"])

                sockets = []
                try:
                    for i in range(2):
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.bind(("127.0.0.1", 0))  # 各自固定源端口 → 不同会话键
                        sockets.append(s)
                        s.sendto(f"pkt-{i}".encode(), ("127.0.0.1", port))
                        assert ws.receive_json()["type"] == "udp_open"
                        assert ws.receive_bytes()[1] == 0x03
                    assert len(server.manager._udp_sessions) == 2

                    # 第三个 addr：丢包（无 udp_open / 帧），连接健康
                    third = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    third.bind(("127.0.0.1", 0))
                    sockets.append(third)
                    third.sendto(b"pkt-3", ("127.0.0.1", port))
                    ws.send_json({"type": "ping"})
                    assert ws.receive_json()["type"] == "pong"
                    assert len(server.manager._udp_sessions) == 2
                finally:
                    for s in sockets:
                        s.close()

    def test_idle_timeout_sweep_sends_udp_close(self):
        """空闲超时回收：sweeper 扫到超时会话 → 下发 udp_close(reason=idle timeout) +
        删映射；未超时会话不被回收"""
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-idle"
        server, app = self._make_server(domain, port, udp_session_timeout=1)

        with TestClient(app) as client:
            token = self._create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                self._auth_ws(ws, token, ["binary_frames", "udp"])

                ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    ext.sendto(b"hello", ("127.0.0.1", port))
                    open_msg = ws.receive_json()
                    assert open_msg["type"] == "udp_open"
                    session_id = open_msg["session_id"]
                    ws.receive_bytes()  # 首帧

                    assert len(server.manager._udp_sessions) == 1

                    # 会话刚活跃（last_seen=now）：即使手动触发 sweep 也不回收
                    # （sweep 在 app loop 里跑不了，这里只验证未超时判定所需的表状态；
                    # 真实回收走 sweeper 周期——timeout=1s → 扫描间隔 1s）
                    time.sleep(2.5)

                    deadline = time.time() + 5
                    close_msg = None
                    while time.time() < deadline:
                        msg = ws.receive_json()
                        if msg["type"] == "udp_close":
                            close_msg = msg
                            break
                    assert close_msg is not None, "空闲超时后应收到 udp_close"
                    assert close_msg["session_id"] == session_id
                    assert close_msg["reason"] == "idle timeout"
                    assert len(server.manager._udp_sessions) == 0

                    # 回收后外部 addr 再发包 → 新会话（新 session_id）
                    ext.sendto(b"again", ("127.0.0.1", port))
                    reopen = ws.receive_json()
                    assert reopen["type"] == "udp_open"
                    assert reopen["session_id"] != session_id
                finally:
                    ext.close()

    def test_sweep_skips_fresh_sessions(self):
        """手动把 last_seen 推老才回收、新鲜会话不误伤（直接驱动 _sweep_udp_sessions）"""
        port = _free_udp_port()
        domain = "udp-sweep-fresh"
        server, _app = self._make_server(domain, port, udp_session_timeout=60)

        from tunely.server import UdpSessionState

        fresh = UdpSessionState(
            session_id=_session_id(), domain=domain, port=port, addr=("10.0.0.1", 5000)
        )
        stale = UdpSessionState(
            session_id=_session_id(), domain=domain, port=port, addr=("10.0.0.2", 5001)
        )
        stale.last_seen = datetime.now() - timedelta(seconds=120)
        server.manager.add_udp_session((port, fresh.addr), fresh)
        server.manager.add_udp_session((port, stale.addr), stale)

        # timeout=60：stale（120s 前）超时；fresh 不动。udp_close 发送因隧道未连接
        # 而尽力而为跳过，不阻塞回收。
        swept = asyncio.run(server._sweep_udp_sessions())
        assert swept == 1
        assert server.manager.get_udp_session_by_addr((port, stale.addr)) is None
        assert server.manager.get_udp_session_by_addr((port, fresh.addr)) is fresh

    def test_client_udp_close_removes_session_and_frame_dropped(self):
        """客户端主动 udp_close：服务端删映射；随后同 session 的帧被丢弃（不断连）"""
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-close"
        server, app = self._make_server(domain, port)

        with TestClient(app) as client:
            token = self._create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                self._auth_ws(ws, token, ["binary_frames", "udp"])

                ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    ext.sendto(b"hello", ("127.0.0.1", port))
                    open_msg = ws.receive_json()
                    session_id = open_msg["session_id"]
                    ws.receive_bytes()

                    ws.send_json({"type": "udp_close", "session_id": session_id})
                    deadline = time.time() + 5
                    while time.time() < deadline:
                        if len(server.manager._udp_sessions) == 0:
                            break
                        time.sleep(0.05)
                    assert len(server.manager._udp_sessions) == 0

                    # 已删映射的会话再来帧：丢弃，连接不断
                    ws.send_bytes(encode_udp_data_frame(session_id, b"late"))
                    ws.send_json({"type": "ping"})
                    assert ws.receive_json()["type"] == "pong"
                finally:
                    ext.close()

    def test_ws_disconnect_cleans_sessions(self):
        """隧道 WS 断连：该隧道全部 UDP 会话清理（unregister → 断连清理路径）"""
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-disc"
        server, app = self._make_server(domain, port)

        with TestClient(app) as client:
            token = self._create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                self._auth_ws(ws, token, ["binary_frames", "udp"])
                ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    ext.sendto(b"hello", ("127.0.0.1", port))
                    assert ws.receive_json()["type"] == "udp_open"
                    ws.receive_bytes()
                    assert len(server.manager._udp_sessions) == 1
                finally:
                    ext.close()
            # with 块退出 = WS 关闭 → unregister → 会话清理
            deadline = time.time() + 5
            while time.time() < deadline:
                if len(server.manager._udp_sessions) == 0:
                    break
                time.sleep(0.05)
            assert len(server.manager._udp_sessions) == 0

    def test_udp_and_tcp_same_port_coexist(self):
        """UDP/TCP 同端口号并存（spec §5）：同端口同时配 tcp_listen 与 udp_listen 正常启动"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from tunely.app import create_lifespan
        from tunely.server import TunnelServer

        port = _free_udp_port()
        domain = "udp-tcp-same"
        server = TunnelServer(
            config=TunnelServerConfig(
                domain="udp.test",
                database_url="sqlite+aiosqlite:///:memory:",
                admin_api_key="k",
                tcp_listen=f"{port}:{domain}",
                udp_listen=f"{port}:{domain}",
            )
        )
        app = FastAPI(lifespan=create_lifespan(server))
        app.include_router(server.router)

        with TestClient(app):
            assert len(server._udp_transports) == 1
            assert len(server._tcp_servers) == 1
            # UDP 面探测：无隧道连接 → 丢包不炸
            ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                ext.sendto(b"probe", ("127.0.0.1", port))
                time.sleep(0.2)
            finally:
                ext.close()
            assert len(server.manager._udp_sessions) == 0

    def test_no_sweeper_when_timeout_disabled(self):
        """udp_session_timeout=0 = 不限时不启动 sweeper"""
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-nosweep"
        server, app = self._make_server(domain, port, udp_session_timeout=0)

        with TestClient(app):
            assert server._udp_sweeper_task is None

    def test_metrics_contains_udp_sessions_active(self):
        """tunely_udp_sessions_active gauge（按 domain）进入 /metrics 渲染"""
        from fastapi.testclient import TestClient

        port = _free_udp_port()
        domain = "udp-metrics"
        server, app = self._make_server(domain, port)

        with TestClient(app) as client:
            token = self._create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                self._auth_ws(ws, token, ["binary_frames", "udp"])
                ext = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    ext.sendto(b"hello", ("127.0.0.1", port))
                    assert ws.receive_json()["type"] == "udp_open"
                    ws.receive_bytes()

                    text = server._render_prometheus_metrics(registered=1)
                    assert "# TYPE tunely_udp_sessions_active gauge" in text
                    assert f'{{domain="{domain}"}} 1' in text
                finally:
                    ext.close()


# ============== 客户端：声明与会话管理 ==============


class _FakeWebSocket:
    """记录 send 调用的假 WS（str=JSON / bytes=0x03 帧）"""

    def __init__(self):
        self.sent: list = []

    async def send(self, data) -> None:
        self.sent.append(data)


class _EchoProtocol(asyncio.DatagramProtocol):
    """测试目标：收到的数据报原样加前缀回发"""

    def __init__(self):
        self.received: list[bytes] = []
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.received.append(data)
        self.transport.sendto(b"echo:" + data, addr)


async def _start_echo_target():
    loop = asyncio.get_event_loop()
    return await loop.create_datagram_endpoint(
        _EchoProtocol, local_addr=("127.0.0.1", 0)
    )


class TestClientUdpSessions:
    def test_client_declares_udp(self):
        """客户端声明已实现的能力：udp（T4）必须在声明集中"""
        caps = TunnelClient._client_capabilities()
        assert "udp" in caps
        assert "binary_frames" in caps
        assert "chunked_http" in caps

    async def test_handle_udp_open_echo_full_cycle(self):
        """udp_open → 建真实 socket；0x03 帧 → 写目标；目标回包 → 0x03 帧回服务端；
        udp_close → 会话清理"""
        transport, protocol = await _start_echo_target()
        try:
            target_addr = transport.get_extra_info("sockname")
            client = TunnelClient(
                server_url="ws://localhost/ws/tunnel",
                token="t",
                target_url=f"http://127.0.0.1:{target_addr[1]}",
            )
            client._negotiated = frozenset({"binary_frames", "udp"})
            ws = _FakeWebSocket()
            client._websocket = ws

            session_id = _session_id()
            await client._handle_udp_open(UdpOpenMessage(session_id=session_id))
            assert session_id in client._udp_sessions

            # 服务端 → 客户端帧：写入目标
            await client._handle_udp_frame(encode_udp_data_frame(session_id, b"hello-udp"))
            await asyncio.wait_for(
                self._wait_until(lambda: len(protocol.received) == 1), timeout=5
            )
            assert protocol.received == [b"hello-udp"]

            # 目标回包 → 客户端 0x03 帧回服务端
            await asyncio.wait_for(
                self._wait_until(lambda: len(ws.sent) == 1), timeout=5
            )
            assert isinstance(ws.sent[0], bytes)
            out_sid, payload = decode_udp_data_frame(ws.sent[0])
            assert out_sid == session_id
            assert payload == b"echo:hello-udp"

            # udp_close：清理会话
            await client._handle_udp_close(
                UdpCloseMessage(session_id=session_id, reason="idle timeout")
            )
            assert session_id not in client._udp_sessions
        finally:
            transport.close()

    async def test_handle_udp_open_ignored_when_not_negotiated(self):
        client = TunnelClient(
            server_url="ws://localhost/ws/tunnel",
            token="t",
            target_url="http://127.0.0.1:9",
        )
        client._negotiated = frozenset({"binary_frames"})  # 未协商 udp
        ws = _FakeWebSocket()
        client._websocket = ws

        await client._handle_udp_open(UdpOpenMessage(session_id=_session_id()))
        assert client._udp_sessions == {}
        assert ws.sent == []

    async def test_handle_udp_frame_unknown_session_dropped(self):
        client = TunnelClient(
            server_url="ws://localhost/ws/tunnel",
            token="t",
            target_url="http://127.0.0.1:9",
        )
        client._negotiated = frozenset({"binary_frames", "udp"})
        ws = _FakeWebSocket()
        client._websocket = ws

        await client._handle_udp_frame(encode_udp_data_frame(_session_id(), b"orphan"))
        assert ws.sent == []

    async def test_close_all_udp_sessions(self):
        """WS 断连收尾：全部会话关闭出表"""
        transport, _protocol = await _start_echo_target()
        try:
            target_addr = transport.get_extra_info("sockname")
            client = TunnelClient(
                server_url="ws://localhost/ws/tunnel",
                token="t",
                target_url=f"http://127.0.0.1:{target_addr[1]}",
            )
            client._negotiated = frozenset({"binary_frames", "udp"})
            client._websocket = _FakeWebSocket()

            await client._handle_udp_open(UdpOpenMessage(session_id=_session_id()))
            await client._handle_udp_open(UdpOpenMessage(session_id=_session_id()))
            assert len(client._udp_sessions) == 2

            await client._close_all_udp_sessions()
            assert client._udp_sessions == {}
        finally:
            transport.close()

    async def test_udp_session_close_is_idempotent(self):
        transport, _protocol = await _start_echo_target()
        try:
            port = transport.get_extra_info("sockname")[1]
            session = UdpSession(_session_id(), _FakeWebSocket())
            assert await session.connect("127.0.0.1", port)
            await session.close()
            await session.close()  # 幂等
            assert session._transport is None
            assert session._pump_task is None
        finally:
            transport.close()

    @staticmethod
    async def _wait_until(predicate, interval: float = 0.02) -> None:
        while not predicate():
            await asyncio.sleep(interval)
