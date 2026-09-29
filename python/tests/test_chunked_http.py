"""
协议 v2 T3 chunked_http 测试（docs/PROTOCOL_V2.md §2）

- 服务端补桥：forward_stream（stream_ok 请求）的非 SSE 目标回 TunnelResponse 时，
  合成 StreamStart + StreamChunk(全量 body, plain) + StreamEnd 投入流队列并清理
  （修复既有缺口：此前只会完成缓冲 future，流式消费侧干等到超时）
- 门控：/forward 缓冲分支零行为变化（恒收 TunnelResponse，wire 带 stream_ok=false）；
  跨隧道串扰校验覆盖流式归属
- py 客户端：三条件门控（request.stream_ok + 协商 chunked_http + 阈值 > 0）；
  Content-Length 已知超阈值直接流式；未知长度越过阈值就地切换；
  文本 plain / 二进制 base64；阈值内照旧回 TunnelResponse（cap 502 语义保留）
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tunely.client import TunnelClient
from tunely.config import TunnelClientConfig, TunnelServerConfig
from tunely.protocol import (
    StreamChunkMessage,
    StreamEndMessage,
    StreamStartMessage,
    TunnelResponse,
    dump_payload,
    parse_message_fast,
    request_payload,
)
from tunely.server import TunnelServer

from .test_server_p1_hardening import _make_responder, _register_conn


@pytest.fixture
async def server():
    srv = TunnelServer(
        config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
    )
    await srv.initialize()
    yield srv
    await srv.close()


# ============== 服务端补桥（TunnelResponse → 流式三段） ==============


class TestServerBridge:
    @pytest.mark.asyncio
    async def test_bridge_synthesizes_stream_triple(self, server):
        """流式 pending 收到 TunnelResponse：合成 Start + 单 Chunk(plain) + End 并清理"""
        _register_conn(server, "br-dom", ws=MagicMock())
        request_id = "req-br-1"
        pending = await server.manager.create_stream_request(request_id, domain="br-dom")

        ok = await server.manager.bridge_response_to_stream(
            TunnelResponse(
                id=request_id,
                status=200,
                headers={"x-a": "b"},
                body="full-body",
                duration_ms=42,
            )
        )
        assert ok is True

        msgs = []
        while True:
            m = pending.queue.get_nowait()
            msgs.append(m)
            if m is None:
                break

        assert isinstance(msgs[0], StreamStartMessage)
        assert msgs[0].status == 200
        assert msgs[0].headers == {"x-a": "b"}
        assert isinstance(msgs[1], StreamChunkMessage)
        assert msgs[1].data == "full-body"
        assert msgs[1].sequence == 0
        assert msgs[1].encoding == "plain"
        assert isinstance(msgs[2], StreamEndMessage)
        assert msgs[2].total_chunks == 1
        assert msgs[2].duration_ms == 42
        assert msgs[2].error is None
        assert msgs[3] is None  # 消费侧结束哨兵
        # 收尾语义：pending 已清理，不再接受后续同名消息
        assert request_id not in server.manager._pending_stream_requests

    @pytest.mark.asyncio
    async def test_bridge_error_response_without_body(self, server):
        """错误响应（无 body）：Start + End(error, total_chunks=0)，无 Chunk"""
        _register_conn(server, "br-err", ws=MagicMock())
        pending = await server.manager.create_stream_request("req-br-2", domain="br-err")

        ok = await server.manager.bridge_response_to_stream(
            TunnelResponse(id="req-br-2", status=500, error="boom", body=None)
        )
        assert ok is True

        msgs = []
        while True:
            m = pending.queue.get_nowait()
            msgs.append(m)
            if m is None:
                break
        assert [type(m).__name__ for m in msgs[:2]] == [
            "StreamStartMessage",
            "StreamEndMessage",
        ]
        assert msgs[0].status == 500
        assert msgs[1].error == "boom"
        assert msgs[1].total_chunks == 0

    @pytest.mark.asyncio
    async def test_bridge_returns_false_for_buffered_pending(self, server):
        """缓冲 pending（/forward）：不走补桥，交回缓冲完成路径"""
        _register_conn(server, "br-buf", ws=MagicMock())
        await server.manager.create_pending_request("req-br-3", domain="br-buf")
        ok = await server.manager.bridge_response_to_stream(
            TunnelResponse(id="req-br-3", status=200, body="x")
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_forward_stream_non_sse_gets_bridged_stream(self, server):
        """端到端：forward_stream × 非 SSE 目标从「超时」变「正常收到全量流」

        mock 客户端按 WS 消息循环同款路径回 TunnelResponse（经
        _handle_client_tunnel_response seam → 命中补桥），消费侧应依次
        收到 StreamStart + 单 Chunk + StreamEnd 而非 Stream timeout。
        """
        body = "full-response-body"

        async def respond(text: str) -> None:
            req = json.loads(text)
            # forward_stream 发出的请求必须带放行标记
            assert req["stream_ok"] is True
            await server._handle_client_tunnel_response(
                TunnelResponse(id=req["id"], status=200, headers={}, body=body),
                "fx-dom",
            )

        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=respond)
        ws.close = AsyncMock()
        _register_conn(server, "fx-dom", ws=ws)

        msgs = []
        async for m in server.forward_stream(
            "fx-dom", method="GET", path="/data", timeout=5.0
        ):
            msgs.append(m)

        assert [type(m).__name__ for m in msgs] == [
            "StreamStartMessage",
            "StreamChunkMessage",
            "StreamEndMessage",
        ]
        assert msgs[1].data == body
        assert msgs[1].encoding == "plain"
        assert msgs[2].total_chunks == 1
        assert msgs[2].error is None

    @pytest.mark.asyncio
    async def test_cross_tunnel_response_rejected_for_stream_pending(self, server):
        """归属校验覆盖流式归属：其他隧道回的 TunnelResponse 被丢弃"""
        _register_conn(server, "own-dom", ws=MagicMock())
        pending = await server.manager.create_stream_request(
            "req-xt", domain="own-dom"
        )
        await server._handle_client_tunnel_response(
            TunnelResponse(id="req-xt", status=200, headers={}, body="stolen"),
            "other-dom",
        )
        assert "req-xt" in server.manager._pending_stream_requests
        assert pending.queue.empty()


# ============== 服务端门控：/forward 缓冲分支零变化 ==============


class TestServerGating:
    @pytest.mark.asyncio
    async def test_forward_buffered_returns_tunnel_response(self, server):
        """/forward（缓冲 API）照旧回 TunnelResponse——不建流式 pending"""
        ws = _make_responder(server, body="buffered-ok")
        _register_conn(server, "buf-dom", ws=ws)

        resp = await server.forward(domain="buf-dom", method="GET", path="/x")
        assert resp.status == 200
        assert resp.body == "buffered-ok"
        assert server.manager._pending_stream_requests == {}

    @pytest.mark.asyncio
    async def test_forward_wire_request_carries_stream_ok_false(self, server):
        """/forward 发出的请求 wire 带 stream_ok=false（缓冲 API 永不放行）"""
        ws = _make_responder(server, body="ok")
        _register_conn(server, "buf-wire", ws=ws)

        await server.forward(domain="buf-wire", method="GET", path="/x")
        sent = json.loads(ws.send_text.call_args.args[0])
        assert sent["stream_ok"] is False

    @pytest.mark.asyncio
    async def test_forward_stream_wire_request_carries_stream_ok_true(self, server):
        """forward_stream 发出的请求 wire 带 stream_ok=true（本测试消费超时流收尾）"""
        ws = MagicMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
        _register_conn(server, "wire-dom", ws=ws)

        consume = asyncio.create_task(
            _consume(server.forward_stream("wire-dom", timeout=0.1))
        )
        await asyncio.wait_for(consume, timeout=2.0)

        sent = json.loads(ws.send_text.call_args.args[0])
        assert sent["stream_ok"] is True


async def _consume(aiter):
    msgs = []
    async for m in aiter:
        msgs.append(m)
    return msgs


# ============== py 客户端：三条件门控 + 流式切换 ==============


class _WsSink:
    """收集 send 调用的假 WS（全部为 JSON 文本）"""

    def __init__(self):
        self.sent: list = []

    async def send(self, data) -> None:
        self.sent.append(data)

    def json_messages(self) -> list[dict]:
        return [json.loads(d) for d in self.sent]


def _make_client(
    handler,
    *,
    negotiated=frozenset({"chunked_http"}),
    threshold=16,
    max_response_bytes=0,
) -> TunnelClient:
    config = TunnelClientConfig(
        server_url="ws://localhost:8000/ws/tunnel",
        token="tok",
        target_url="http://127.0.0.1:1",
        max_response_bytes=max_response_bytes,
        stream_threshold_bytes=threshold,
    )
    client = TunnelClient(config=config)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._websocket = _WsSink()
    client._negotiated = frozenset(negotiated)
    return client


def _req(request_id: str = "req-1", stream_ok: bool = False):
    return parse_message_fast(
        dump_payload(
            request_payload(request_id, "GET", "/", {}, None, 30.0, stream_ok=stream_ok)
        )
    )


class TestClientGating:
    def test_client_declares_chunked_http(self):
        """py 客户端声明已实现的 chunked_http 能力"""
        assert "chunked_http" in TunnelClient._client_capabilities()

    @pytest.mark.asyncio
    async def test_not_negotiated_buffers_response(self):
        """未协商 chunked_http：照旧回 TunnelResponse（不发任何流式消息）"""
        body = "x" * 100

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=body)

        client = _make_client(handler, negotiated=frozenset())
        resp = await client._execute_request(_req(stream_ok=True))
        assert resp is not None
        assert resp.status == 200 and resp.body == body
        assert client._websocket.sent == []

    @pytest.mark.asyncio
    async def test_stream_ok_false_buffers_response(self):
        """服务端未放行（stream_ok=False）：照旧缓冲，即使已协商"""
        body = "x" * 100

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=body)

        client = _make_client(handler)
        resp = await client._execute_request(_req(stream_ok=False))
        assert resp is not None and resp.body == body
        assert client._websocket.sent == []

    @pytest.mark.asyncio
    async def test_zero_threshold_never_streams(self):
        """阈值=0 = 从不流式"""
        body = "x" * 100

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=body)

        client = _make_client(handler, threshold=0)
        resp = await client._execute_request(_req(stream_ok=True))
        assert resp is not None and resp.body == body
        assert client._websocket.sent == []

    @pytest.mark.asyncio
    async def test_within_threshold_keeps_buffered_response(self):
        """已知长度但未超阈值：不切换，照旧回 TunnelResponse"""
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="small")

        client = _make_client(handler)
        resp = await client._execute_request(_req(stream_ok=True))
        assert resp is not None
        assert resp.status == 200 and resp.body == "small"
        assert client._websocket.sent == []

    @pytest.mark.asyncio
    async def test_unknown_length_within_threshold_keeps_buffered(self):
        """未知长度但全程未超阈值：照旧回 TunnelResponse"""
        async def handler(request: httpx.Request) -> httpx.Response:
            async def gen():
                yield b"tiny"

            return httpx.Response(200, content=gen())

        client = _make_client(handler)
        resp = await client._execute_request(_req(stream_ok=True))
        assert resp is not None and resp.body == "tiny"
        assert client._websocket.sent == []

    @pytest.mark.asyncio
    async def test_cap_502_semantics_preserved(self):
        """cap < 阈值：超 cap 仍按既有语义 502 中止（不切流式）"""
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="z" * 50)

        client = _make_client(handler, threshold=100, max_response_bytes=10)
        resp = await client._execute_request(_req(stream_ok=True))
        assert resp is not None
        assert resp.status == 502
        assert "client cap" in (resp.error or "")
        assert client._websocket.sent == []


class TestClientStreaming:
    @pytest.mark.asyncio
    async def test_known_length_switches_to_stream_plain(self):
        """Content-Length 已知且超阈值：直接流式（JSON 文本 → plain）"""
        big = "y" * 100

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=big, headers={"content-type": "application/json"}
            )

        client = _make_client(handler)
        resp = await client._execute_request(_req(stream_ok=True))
        assert resp is None  # 流式消息已发送，不回 TunnelResponse

        msgs = client._websocket.json_messages()
        assert [m["type"] for m in msgs] == [
            "stream_start",
            "stream_chunk",
            "stream_end",
        ]
        assert msgs[0]["status"] == 200
        assert msgs[1]["data"] == big
        assert msgs[1]["encoding"] == "plain"
        assert msgs[1]["sequence"] == 0
        assert msgs[2]["total_chunks"] == 1
        assert msgs[2]["error"] is None

    @pytest.mark.asyncio
    async def test_binary_content_streams_base64(self):
        """二进制 content-type：base64 编码块，可无损解码回原始字节"""
        payload = bytes(range(256)) * 4  # 1024 字节二进制

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=payload,
                headers={"content-type": "application/octet-stream"},
            )

        client = _make_client(handler)
        resp = await client._execute_request(_req(stream_ok=True))
        assert resp is None

        msgs = client._websocket.json_messages()
        assert [m["type"] for m in msgs] == [
            "stream_start",
            "stream_chunk",
            "stream_end",
        ]
        assert msgs[1]["encoding"] == "base64"
        assert base64.b64decode(msgs[1]["data"]) == payload
        assert msgs[2]["total_chunks"] == 1

    @pytest.mark.asyncio
    async def test_unknown_length_switches_in_place(self):
        """Content-Length 未知：读到一半越过阈值就地切换，已缓冲字节作首批"""
        part1 = "a" * 10
        part2 = "b" * 10  # 累计 20 > 阈值 16，在 part2 越线

        async def handler(request: httpx.Request) -> httpx.Response:
            async def gen():
                yield part1.encode()
                yield part2.encode()

            return httpx.Response(
                200, content=gen(), headers={"content-type": "text/plain"}
            )

        client = _make_client(handler)
        resp = await client._execute_request(_req(stream_ok=True))
        assert resp is None

        msgs = client._websocket.json_messages()
        assert [m["type"] for m in msgs] == [
            "stream_start",
            "stream_chunk",
            "stream_end",
        ]
        # 首块 = 越线前已缓冲字节 + 越线块；随后续流到结束（此处恰好是末尾）
        assert msgs[1]["data"] == part1 + part2
        assert msgs[1]["encoding"] == "plain"
        assert msgs[2]["total_chunks"] == 1
