"""
0.7.3 性能批测试

- payload_* 构造器与 pydantic 模型 wire 形状逐字段一致；dump_payload 紧凑无空格
- parse_message_fast：热类型直构与 pydantic 全校验等价；畸形消息 ValueError（F10 语义）
- forward_stream：断连哨兵立即终止（不等超时）、超时兜底、per-chunk 不再建 task
- py 客户端：httpx 客户端实例级复用；响应体内存上限（超限 502 中止）
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import tunely
from tunely.config import TunnelClientConfig, TunnelServerConfig
from tunely.client import TunnelClient
from tunely.protocol import (
    StreamChunkMessage,
    StreamEndMessage,
    StreamStartMessage,
    TunnelRequest,
    TunnelResponse,
    TcpCloseMessage,
    TcpConnectMessage,
    TcpDataMessage,
    dump_payload,
    parse_message,
    parse_message_fast,
    request_payload,
    response_payload,
    stream_chunk_payload,
    stream_end_payload,
    stream_start_payload,
    tcp_close_payload,
    tcp_connect_payload,
    tcp_data_payload,
)
from tunely.server import TunnelServer

from .test_server_p1_hardening import _make_responder, _register_conn

# payload 函数与对应模型类的对照表
_PAYLOAD_TABLE = [
    (request_payload("r1", "GET", "/x", {"a": "b"}, "body", 12.0), TunnelRequest),
    # 协议 v2 T3：stream_ok / encoding 非默认值也要与 model_dump 键集一致
    (request_payload("r2", "POST", "/y", {}, None, 5.0, stream_ok=True), TunnelRequest),
    (response_payload("r1", 200, {"a": "b"}, "ok", None, 12), TunnelResponse),
    (stream_start_payload("r1", 200, {"a": "b"}), StreamStartMessage),
    (stream_chunk_payload("r1", "chunk-数据", 3), StreamChunkMessage),
    (stream_chunk_payload("r2", "aGk=", 1, encoding="base64"), StreamChunkMessage),
    (stream_end_payload("r1", None, 100, 5), StreamEndMessage),
    (tcp_connect_payload("c1"), TcpConnectMessage),
    (tcp_data_payload("c1", "aGk=", 7), TcpDataMessage),
    (tcp_close_payload("c1", None), TcpCloseMessage),
]


class TestPayloadWireShape:
    @pytest.mark.parametrize("payload,model_cls", _PAYLOAD_TABLE)
    def test_key_set_matches_pydantic_dump(self, payload, model_cls):
        """payload 键集与 pydantic model_dump 逐字段一致（wire 不变）"""
        model_keys = set(model_cls.model_fields.keys())
        assert set(payload.keys()) == model_keys

    @pytest.mark.parametrize("payload,model_cls", _PAYLOAD_TABLE)
    def test_roundtrip_via_fast_parse(self, payload, model_cls):
        """dump_payload → parse_message_fast 与 pydantic 全校验路径等价"""
        via_pydantic = parse_message(model_cls(**payload).model_dump())
        via_fast = parse_message_fast(dump_payload(payload))
        assert type(via_fast) is type(via_pydantic)
        assert via_fast.model_dump() == via_pydantic.model_dump()

    def test_dump_payload_is_compact(self):
        """dump_payload 无空格（与 pydantic model_dump_json 输出形状一致）"""
        raw = dump_payload(tcp_data_payload("c1", "aGk=", 1))
        assert " " not in raw
        assert '","' in raw

    def test_fast_parse_rejects_malformed(self):
        """类型不对的畸形消息 → ValueError（调用方按 F10 丢弃）"""
        bad_cases = [
            '{"type":"response","id":"x","status":"bad"}',  # status 非法
            '{"type":"response","status":200}',  # 缺 id
            '{"type":"tcp_data","conn_id":"c","data":123}',  # data 非法
            '{"type":"request"}',  # 缺 method/path
            '["not","an","object"]',  # 非 object
            'not-json-at-all',  # 非 JSON（JSONDecodeError ⊂ ValueError）
        ]
        for raw in bad_cases:
            with pytest.raises(ValueError):
                parse_message_fast(raw)

    def test_fast_parse_fallback_keeps_full_validation(self):
        """冷门类型（auth 等）回退 parse_message 全量 pydantic 校验"""
        msg = parse_message_fast('{"type":"auth","token":"t"}')
        assert type(msg).__name__ == "AuthMessage"
        assert msg.client_version == "unknown"


# ============== forward_stream：哨兵失败 / 超时 ==============


@pytest.fixture
async def server():
    srv = TunnelServer(
        config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
    )
    await srv.initialize()
    yield srv
    await srv.close()


class TestForwardStreamSentinel:
    @pytest.mark.asyncio
    async def test_disconnect_ends_stream_immediately(self, server):
        """断连哨兵立即终止流（不等超时兜底）"""
        ws = _make_responder(server)
        conn = _register_conn(server, "s-dom", ws=ws)

        async def consume():
            msgs = []
            async for m in server.forward_stream(
                "s-dom", method="GET", path="/", timeout=30.0
            ):
                msgs.append(m)
            return msgs

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # 等 forward_stream 进入等待
        await server.manager.unregister(conn.token, websocket=ws)

        msgs = await asyncio.wait_for(task, timeout=2.0)
        assert msgs[-1].error == "tunnel disconnected"

    @pytest.mark.asyncio
    async def test_explicit_fail_ends_stream(self, server):
        """fail_stream_request 经哨兵唤醒消费侧"""
        ws = _make_responder(server)
        _register_conn(server, "s-dom2", ws=ws)

        async def consume():
            msgs = []
            async for m in server.forward_stream(
                "s-dom2", method="GET", path="/", timeout=30.0
            ):
                msgs.append(m)
            return msgs

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        pending_id = next(iter(server.manager._pending_stream_requests.keys()))
        await server.manager.fail_stream_request(pending_id, "boom")

        msgs = await asyncio.wait_for(task, timeout=2.0)
        assert msgs[-1].error == "boom"

    @pytest.mark.asyncio
    async def test_queue_overflow_ends_stream(self, server):
        """队列溢出：哨兵挤掉一条滞留 chunk 后仍能终止（不死锁、不悬挂）"""
        ws = _make_responder(server)
        _register_conn(server, "s-dom3", ws=ws)
        server.manager.stream_queue_maxsize = 2

        async def consume():
            msgs = []
            async for m in server.forward_stream(
                "s-dom3", method="GET", path="/", timeout=30.0
            ):
                msgs.append(m)
            return msgs

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        pending = next(iter(server.manager._pending_stream_requests.values()))
        # 先投 StreamStart（进入 pending.started），再灌满队列触发溢出
        await server.manager.handle_stream_start(
            StreamStartMessage(id=pending.request_id, status=200, headers={})
        )
        for i in range(5):
            await server.manager.handle_stream_chunk(
                StreamChunkMessage(id=pending.request_id, data=f"c{i}", sequence=i)
            )

        msgs = await asyncio.wait_for(task, timeout=2.0)
        assert msgs[-1].error == "stream queue overflow"

    @pytest.mark.asyncio
    async def test_timeout_still_ends_stream(self, server):
        """无消息时超时兜底仍然生效"""
        ws = _make_responder(server)
        _register_conn(server, "s-dom4", ws=ws)

        async def consume():
            msgs = []
            async for m in server.forward_stream(
                "s-dom4", method="GET", path="/", timeout=0.1
            ):
                msgs.append(m)
            return msgs

        msgs = await asyncio.wait_for(consume(), timeout=2.0)
        assert msgs[-1].error == "Stream timeout"


# ============== py 客户端：连接复用 + 响应上限 ==============


def _make_http_client(target_hits: list) -> TunnelClient:
    config = TunnelClientConfig(
        server_url="ws://localhost:8000/ws/tunnel",
        token="tok",
        target_url="http://127.0.0.1:1",  # 不真正发起：走 MockTransport
        max_response_bytes=0,
    )
    client = TunnelClient(config=config)

    async def handler(request: httpx.Request) -> httpx.Response:
        target_hits.append(request)
        return httpx.Response(200, text="ok")

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _make_request(path="/"):
    return parse_message_fast(
        dump_payload(request_payload("req-1", "GET", path, {}, None, 30.0))
    )


class TestHttpClientReuse:
    @pytest.mark.asyncio
    async def test_shared_client_reused_across_requests(self):
        """两次请求共用同一 AsyncClient（连接池复用）"""
        hits: list = []
        client = _make_http_client(hits)
        try:
            req = _make_request()
            await client._execute_request(req)
            await client._execute_request(req)
            assert len(hits) == 2
            assert client._http is not None and not client._http.is_closed
        finally:
            await client.stop()
        assert client._http.is_closed  # stop() 关闭共享客户端

    @pytest.mark.asyncio
    async def test_oversized_response_capped_502(self):
        """响应体超上限：中止读取，返回 502（不再全量缓冲）"""
        hits: list = []
        config = TunnelClientConfig(
            server_url="ws://localhost:8000/ws/tunnel",
            token="tok",
            target_url="http://127.0.0.1:1",
            max_response_bytes=10,
        )
        client = TunnelClient(config=config)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="x" * 1000)

        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resp = await client._execute_request(_make_request())
        assert resp is not None
        assert resp.status == 502
        assert "client cap" in (resp.error or "")
        await client.stop()

    @pytest.mark.asyncio
    async def test_normal_response_passes_with_cap(self):
        """上限内响应正常返回"""
        hits: list = []
        config = TunnelClientConfig(
            server_url="ws://localhost:8000/ws/tunnel",
            token="tok",
            target_url="http://127.0.0.1:1",
            max_response_bytes=1000,
        )
        client = TunnelClient(config=config)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="hello")

        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resp = await client._execute_request(_make_request())
        assert resp is not None
        assert resp.status == 200
        assert resp.body == "hello"
        await client.stop()
