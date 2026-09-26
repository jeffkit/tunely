"""
稳定性/性能批次 P1 修复测试（0.7.0）

- 1. 无界缓冲上界：HTTP 触发的 TCP 转发累积缓冲上限（tcp_forward_max_buffer_bytes）、
     流式队列上界（stream_queue_maxsize + QueueFull 按流错误结束）
- 2. F8 断连时在途请求悬挂：unregister 立刻失败该域的在途 pending
     （普通请求 future / 流式 error / TCP 转发错误完成），不误清真实 TCP 连接
- 3. F10 畸形消息不断隧道：WS 消息循环里非法 JSON / 未知类型只丢弃该条
- 4. F13 flush 顺序：逐域名写库后立即推进该域快照，单域失败不整批重发
- 5. F17 falsy body 丢弃：body 为 {} / 0 / "" / False 时必须保留
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from tunely.config import TunnelServerConfig
from tunely.protocol import (
    AuthMessage,
    PingMessage,
    StreamChunkMessage,
    StreamEndMessage,
    StreamStartMessage,
    TunnelResponse,
)
from tunely.repository import TunnelRepository, TunnelRequestLogRepository
from tunely.server import (
    ActiveConnection,
    CreateTunnelRequest,
    TunnelManager,
    TunnelServer,
)

# ============== 测试辅助 ==============


def _make_ws(receive_texts: list) -> AsyncMock:
    mock_ws = AsyncMock()
    mock_ws.accept = AsyncMock()
    mock_ws.send_text = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.receive_text = AsyncMock(side_effect=receive_texts)
    return mock_ws


def _make_db_repo(get_by_token_return=None):
    mock_repo = AsyncMock()
    mock_repo.get_by_token = AsyncMock(return_value=get_by_token_return)
    mock_repo.update_last_connected = AsyncMock()

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_db = MagicMock()
    mock_db.session = MagicMock(return_value=mock_session)
    return mock_db, mock_repo


def _register_conn(server: TunnelServer, domain: str, token: str | None = None, ws=None):
    """在 manager 里直接注册一个 mock 客户端连接（挂上转发面）"""
    token = token or f"tok-{domain}"
    if ws is None:
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
    conn = ActiveConnection(
        websocket=ws, tunnel_id=1, domain=domain, token=token
    )
    server.manager._connections[token] = conn
    server.manager._domain_token_map[domain] = token
    return conn


def _make_responder(server: TunnelServer, status: int = 200, headers=None, body="ok"):
    """mock 一个收到 TunnelRequest 后立即回 TunnelResponse 的隧道客户端"""
    async def respond(text: str) -> None:
        req = json.loads(text)
        await server.manager.complete_request(
            req["id"],
            TunnelResponse(id=req["id"], status=status, headers=headers or {}, body=body),
        )

    ws = MagicMock()
    ws.send_text = AsyncMock(side_effect=respond)
    ws.close = AsyncMock()
    return ws


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.fixture
async def server():
    srv = TunnelServer(
        config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
    )
    await srv.initialize()
    yield srv
    await srv.close()


# ============== 1. TCP 转发缓冲上限 ==============


class TestTcpForwardBufferLimit:
    def _manager(self, limit: int) -> TunnelManager:
        return TunnelManager(tcp_forward_max_buffer_bytes=limit)

    @pytest.mark.asyncio
    async def test_over_limit_completes_with_error(self):
        manager = self._manager(100)
        future = await manager.create_pending_tcp_request("c1", domain="d")

        await manager.handle_tcp_response_data("c1", b"x" * 60)
        await manager.handle_tcp_response_data("c1", b"x" * 60)  # 累计 120 > 100

        assert future.done()
        assert future.result() == {"error": "response too large", "data": b""}
        assert "c1" not in manager._pending_tcp_requests

    @pytest.mark.asyncio
    async def test_at_limit_still_accumulates(self):
        manager = self._manager(100)
        future = await manager.create_pending_tcp_request("c1", domain="d")

        await manager.handle_tcp_response_data("c1", b"a" * 50)
        await manager.handle_tcp_response_data("c1", b"b" * 50)  # 恰好 100，不超

        assert not future.done()
        assert "c1" in manager._pending_tcp_requests

        await manager.complete_tcp_request("c1")
        assert future.result() == {"error": None, "data": b"a" * 50 + b"b" * 50}

    @pytest.mark.asyncio
    async def test_zero_disables_limit(self):
        manager = self._manager(0)
        future = await manager.create_pending_tcp_request("c1", domain="d")

        await manager.handle_tcp_response_data("c1", b"x" * 1000)
        await manager.handle_tcp_response_data("c1", b"x" * 1000)

        assert not future.done()
        assert manager._pending_tcp_requests["c1"].total_bytes == 2000

    def test_default_limit_is_10mb(self):
        assert TunnelManager().tcp_forward_max_buffer_bytes == 10485760
        assert TunnelManager().stream_queue_maxsize == 1024

    def test_config_defaults_and_env(self, monkeypatch):
        cfg = TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        assert cfg.tcp_forward_max_buffer_bytes == 10485760
        assert cfg.stream_queue_maxsize == 1024

        monkeypatch.setenv("WS_TUNNEL_TCP_FORWARD_MAX_BUFFER_BYTES", "2048")
        monkeypatch.setenv("WS_TUNNEL_STREAM_QUEUE_MAXSIZE", "8")
        cfg2 = TunnelServerConfig()
        assert cfg2.tcp_forward_max_buffer_bytes == 2048
        assert cfg2.stream_queue_maxsize == 8

    def test_server_wires_config_into_manager(self):
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                tcp_forward_max_buffer_bytes=123,
                stream_queue_maxsize=7,
            )
        )
        assert srv.manager.tcp_forward_max_buffer_bytes == 123
        assert srv.manager.stream_queue_maxsize == 7

    @pytest.mark.asyncio
    async def test_forward_tcp_returns_502_on_oversize(self):
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                tcp_forward_max_buffer_bytes=100,
            )
        )
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
        _register_conn(srv, "tcp-big", ws=ws)

        task = asyncio.create_task(
            srv._forward_tcp(domain="tcp-big", body=b"ping", timeout=5)
        )
        assert await _wait_until(lambda: srv.manager._pending_tcp_requests)
        conn_id = next(iter(srv.manager._pending_tcp_requests))

        await srv.manager.handle_tcp_response_data(conn_id, b"x" * 101)

        result = await asyncio.wait_for(task, timeout=2)
        assert result.status == 502
        assert result.error == "response too large"


# ============== 1b. 流式队列上界 ==============


class TestStreamQueueBound:
    @pytest.mark.asyncio
    async def test_queue_maxsize_applied(self):
        manager = TunnelManager(stream_queue_maxsize=3)
        pending = await manager.create_stream_request("r1")
        assert pending.queue.maxsize == 3

        manager0 = TunnelManager(stream_queue_maxsize=0)
        pending0 = await manager0.create_stream_request("r2")
        assert pending0.queue.maxsize == 0  # 0 = 不限

    @pytest.mark.asyncio
    async def test_overflow_marks_stream_failed(self):
        manager = TunnelManager(stream_queue_maxsize=1)
        pending = await manager.create_stream_request("r1", domain="d")

        assert await manager.handle_stream_start(
            StreamStartMessage(id="r1", status=200, headers={})
        )
        # 队列已满（maxsize=1），下一个 chunk 触发 QueueFull
        assert not await manager.handle_stream_chunk(
            StreamChunkMessage(id="r1", data="x", sequence=0)
        )
        assert pending.ended
        assert pending.error == "stream queue overflow"
        assert pending.failed.is_set()

    @pytest.mark.asyncio
    async def test_unbounded_when_zero(self):
        manager = TunnelManager(stream_queue_maxsize=0)
        pending = await manager.create_stream_request("r1", domain="d")
        assert await manager.handle_stream_start(
            StreamStartMessage(id="r1", status=200, headers={})
        )
        for i in range(5000):
            assert await manager.handle_stream_chunk(
                StreamChunkMessage(id="r1", data="x", sequence=i)
            )
        assert not pending.failed.is_set()

    @pytest.mark.asyncio
    async def test_consumer_gets_error_end_immediately(self):
        """溢出后消费侧立刻收到带错误的 StreamEndMessage，不等超时"""
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                stream_queue_maxsize=1,
            )
        )
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
        _register_conn(srv, "s-ovf", ws=ws)

        messages = []

        async def consume():
            async for m in srv.forward_stream(
                domain="s-ovf", method="GET", path="/", timeout=30
            ):
                messages.append(m)

        task = asyncio.create_task(consume())
        assert await _wait_until(lambda: srv.manager._pending_stream_requests)
        rid = next(iter(srv.manager._pending_stream_requests))

        rid_obj = srv.manager._pending_stream_requests[rid]
        await srv.manager.handle_stream_start(
            StreamStartMessage(id=rid, status=200, headers={})
        )
        await srv.manager.handle_stream_chunk(
            StreamChunkMessage(id=rid, data="x", sequence=0)
        )
        assert rid_obj.failed.is_set()

        await asyncio.wait_for(task, timeout=3)  # 远小于 30s 即结束

        # 溢出即整流失败：丢弃已积压消息，立刻以错误结束（fail fast）
        assert len(messages) == 1
        assert isinstance(messages[0], StreamEndMessage)
        assert messages[0].error == "stream queue overflow"

    @pytest.mark.asyncio
    async def test_fail_request_terminates_stream(self):
        manager = TunnelManager(stream_queue_maxsize=4)
        await manager.create_stream_request("r1", domain="d")

        assert await manager.fail_request("r1", "Request timeout")
        assert "r1" not in manager._pending_stream_requests
        # fail_request 已 pop，通过返回值与字典判断；再验证 failed 事件语义
        pending = await manager.create_stream_request("r2", domain="d")
        await manager.fail_stream_request("r2", "boom")
        assert pending.error == "boom"
        assert pending.failed.is_set()
        assert "r2" not in manager._pending_stream_requests


# ============== 2. F8 断连时在途请求悬挂 ==============


class TestDisconnectFailsPendingRequests:
    @pytest.mark.asyncio
    async def test_unregister_fails_all_pending_kinds(self):
        manager = TunnelManager()
        ws = MagicMock()
        await manager.register(ws, 1, "f8-dom", "tok")

        req_future = await manager.create_pending_request("req-1", domain="f8-dom")
        stream = await manager.create_stream_request("stream-1", domain="f8-dom")
        tcp_future = await manager.create_pending_tcp_request("conn-1", domain="f8-dom")

        await manager.unregister("tok", websocket=ws)

        # 普通请求：future 以 ConnectionError 结束
        assert req_future.done()
        assert isinstance(req_future.exception(), ConnectionError)
        assert "tunnel disconnected" in str(req_future.exception())
        assert "req-1" not in manager._pending_requests

        # 流式请求：error + failed 事件，字典清理
        assert stream.error == "tunnel disconnected"
        assert stream.failed.is_set()
        assert "stream-1" not in manager._pending_stream_requests

        # TCP 转发：以错误完成
        assert tcp_future.done()
        assert tcp_future.result() == {"error": "tunnel disconnected", "data": b""}
        assert "conn-1" not in manager._pending_tcp_requests

    @pytest.mark.asyncio
    async def test_unregister_keeps_real_tcp_connections(self):
        """真实 TCP 连接（TcpConnectionState）由连接自身生命周期管理，不误清"""
        manager = TunnelManager()
        ws = MagicMock()
        await manager.register(ws, 1, "f8-dom", "tok")

        reader = asyncio.StreamReader()
        writer = MagicMock()
        await manager.register_tcp_connection("tcp-real", "f8-dom", reader, writer, ws)

        await manager.unregister("tok", websocket=ws)

        tcp_real = await manager.get_tcp_connection("tcp-real")
        assert tcp_real is not None

    @pytest.mark.asyncio
    async def test_unregister_other_domain_pendings_untouched(self):
        manager = TunnelManager()
        ws = MagicMock()
        await manager.register(ws, 1, "f8-dom", "tok")

        other_future = await manager.create_pending_request("req-other", domain="elsewhere")
        no_domain_future = await manager.create_pending_request("req-nodomain")

        await manager.unregister("tok", websocket=ws)

        assert not other_future.done()
        assert not no_domain_future.done()

    @pytest.mark.asyncio
    async def test_unregister_identity_mismatch_keeps_pendings(self):
        """0.6.1 语义保持：身份不匹配（已被新连接接管）不注销、不动 pending"""
        manager = TunnelManager()
        ws = MagicMock()
        await manager.register(ws, 1, "f8-dom", "tok")
        future = await manager.create_pending_request("req-1", domain="f8-dom")

        impostor = MagicMock()
        await manager.unregister("tok", websocket=impostor)

        assert manager.get_connection_by_token("tok") is not None
        assert not future.done()

    @pytest.mark.asyncio
    async def test_forward_http_returns_500_quickly_after_disconnect(self):
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
        conn = _register_conn(srv, "f8-e2e", ws=ws)

        async def do_forward():
            return await srv.forward(domain="f8-e2e", method="GET", path="/", timeout=30)

        task = asyncio.create_task(do_forward())
        assert await _wait_until(lambda: srv.manager.pending_requests_count() > 0)

        await srv.manager.unregister(conn.token, websocket=ws)

        resp = await asyncio.wait_for(task, timeout=2)
        assert resp.status == 500
        assert "tunnel disconnected" in (resp.error or "")


# ============== 3. F10 畸形消息不断隧道 ==============


class TestMalformedMessageTolerance:
    @pytest.mark.asyncio
    async def test_malformed_messages_do_not_kill_tunnel(self):
        server = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        tunnel = MagicMock()
        tunnel.enabled = True
        tunnel.id = 1
        tunnel.domain = "f10-dom"
        mock_db, mock_repo = _make_db_repo(get_by_token_return=tunnel)
        server.db = mock_db

        ws = _make_ws([
            AuthMessage(token="tok-f10").model_dump_json(),
            "this is not json",                      # 非法 JSON
            '{"type": "definitely_unknown_type"}',   # 未知消息类型
            '{"type": "tunnel_response"}',           # 缺字段的合法类型骨架
            PingMessage().model_dump_json(),         # 合法消息仍被处理
            WebSocketDisconnect(code=1000),
        ])

        with patch("tunely.server.TunnelRepository", return_value=mock_repo):
            await server._handle_websocket(ws)

        # 断连后连接仍存活时才可能回 Pong：畸形消息后隧道未断
        sent_texts = [c.args[0] for c in ws.send_text.await_args_list]
        assert any('"type":"pong"' in t.replace(" ", "") for t in sent_texts)
        # 未走认证失败/错误关闭路径
        ws.close.assert_not_called()
        # 畸形消息未产生任何发送（只有 auth_ok + pong）
        assert len(sent_texts) == 2

    @pytest.mark.asyncio
    async def test_malformed_then_valid_request_flow(self):
        """畸形消息后，后续 TunnelResponse 仍能完成在途请求"""
        server = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        tunnel = MagicMock()
        tunnel.enabled = True
        tunnel.id = 1
        tunnel.domain = "f10-dom"
        mock_db, mock_repo = _make_db_repo(get_by_token_return=tunnel)
        server.db = mock_db

        future = await server.manager.create_pending_request("rid-1", domain="f10-dom")
        response_payload = TunnelResponse(
            id="rid-1", status=200, headers={}, body="ok"
        ).model_dump_json()

        ws = _make_ws([
            AuthMessage(token="tok-f10").model_dump_json(),
            "{broken json",
            response_payload,
            WebSocketDisconnect(code=1000),
        ])

        with patch("tunely.server.TunnelRepository", return_value=mock_repo):
            await server._handle_websocket(ws)

        assert future.done()
        assert future.result().status == 200


# ============== 4. F13 flush 顺序 ==============


class TestFlushSnapshotPerDomain:
    @pytest.mark.asyncio
    async def test_flush_advances_snapshot_per_domain_and_recovers(self, server):
        await server._create_tunnel(CreateTunnelRequest(domain="flush-a"), api_key=None)
        await server._create_tunnel(CreateTunnelRequest(domain="flush-b"), api_key=None)

        server._tunnel_bytes["flush-a"] = {"bytes_in": 100, "bytes_out": 0}
        server._tunnel_bytes["flush-b"] = {"bytes_in": 0, "bytes_out": 50}

        real_increment = TunnelRepository.increment_tunnel_bytes

        async def flaky_increment(repo_self, domain, delta_in, delta_out):
            if domain == "flush-a":
                raise RuntimeError("db boom")
            return await real_increment(repo_self, domain, delta_in, delta_out)

        with patch.object(
            TunnelRepository, "increment_tunnel_bytes", flaky_increment
        ):
            await server._flush_tunnel_bytes()

        # 成功域：快照已推进
        assert server._flushed_bytes["flush-b"] == {"bytes_in": 0, "bytes_out": 50}
        # 失败域：快照未推进（等待下轮重试）
        flushed_a = server._flushed_bytes.get("flush-a", {"bytes_in": 0, "bytes_out": 0})
        assert flushed_a == {"bytes_in": 0, "bytes_out": 0}

        # 恢复后再 flush：失败域补写一次，成功域不重复累加
        await server._flush_tunnel_bytes()
        assert server._flushed_bytes["flush-a"] == {"bytes_in": 100, "bytes_out": 0}

        async with server.db.session() as session:
            repo = TunnelRepository(session)
            row_a = await repo.get_by_domain("flush-a")
            row_b = await repo.get_by_domain("flush-b")
        assert row_a.bytes_in == 100
        assert row_b.bytes_out == 50  # 未重复累加成 100

    @pytest.mark.asyncio
    async def test_flush_noop_when_no_delta(self, server):
        server._tunnel_bytes["idle"] = {"bytes_in": 10, "bytes_out": 10}
        server._flushed_bytes["idle"] = {"bytes_in": 10, "bytes_out": 10}
        await server._flush_tunnel_bytes()  # 不抛异常即通过


# ============== 5. F17 falsy body 丢弃 ==============


class TestFalsyBodyPreserved:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "falsy_body,wire_body",
        [
            ({}, "{}"),
            (0, "0"),
            (False, "false"),
            ("", '""'),
            (None, None),
        ],
    )
    async def test_forward_http_body_wire_fidelity(self, falsy_body, wire_body):
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        ws = _make_responder(srv)
        _register_conn(srv, "f17", ws=ws)

        resp = await srv.forward(
            domain="f17", method="POST", path="/x", body=falsy_body
        )
        assert resp.status == 200

        sent = json.loads(ws.send_text.await_args.args[0])
        assert sent["body"] == wire_body

    @pytest.mark.asyncio
    async def test_forward_stream_body_wire_fidelity(self):
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
        _register_conn(srv, "f17-stream", ws=ws)

        gen = srv.forward_stream(
            domain="f17-stream", method="POST", path="/x", body={}
        )
        task = asyncio.create_task(gen.__anext__())
        assert await _wait_until(lambda: srv.manager._pending_stream_requests)

        sent = json.loads(ws.send_text.await_args.args[0])
        assert sent["body"] == "{}"

        rid = next(iter(srv.manager._pending_stream_requests))
        await srv.manager.fail_stream_request(rid, "test-end")
        await asyncio.wait_for(task, timeout=2)
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_forward_tcp_body_wire_fidelity(self):
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
        _register_conn(srv, "f17-tcp", ws=ws)

        task = asyncio.create_task(
            srv._forward_tcp(domain="f17-tcp", body=0, timeout=5)
        )
        assert await _wait_until(lambda: srv.manager._pending_tcp_requests)
        conn_id = next(iter(srv.manager._pending_tcp_requests))

        sent = [json.loads(c.args[0]) for c in ws.send_text.await_args_list]
        data_msgs = [m for m in sent if m["type"] == "tcp_data"]
        assert len(data_msgs) == 1
        assert base64.b64decode(data_msgs[0]["data"]) == b"0"

        await srv.manager.complete_tcp_request(conn_id)
        resp = await asyncio.wait_for(task, timeout=2)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_repo_log_preserves_falsy_bodies(self, db_manager):
        async with db_manager.session() as session:
            repo = TunnelRequestLogRepository(session)
            log = await repo.create(
                tunnel_domain="f17",
                method="GET",
                path="/",
                request_headers={},
                request_body="",
                status_code=200,
                response_headers={},
                response_body="0",
            )
            assert log.request_headers == "{}"
            assert log.request_body == ""
            assert log.response_headers == "{}"
            assert log.response_body == "0"

            d = log.to_dict()
            assert d["request_body"] == ""
            assert d["response_body"] == "0"
            assert d["request_headers"] == {}
