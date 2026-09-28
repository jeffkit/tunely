"""
0.7.1 转发热路径性能改造测试

- 请求计数走内存增量 + 周期批量落库（转发热路径零 DB 写，live 展示含增量）
- 请求日志后台队列落库：队列满丢弃并计数（不反压转发面）
- 请求日志保留策略：超期行被清理任务删除，0 = 永久保留
- HTTP 模式响应体大小上限：超限 502，不再全量缓冲
- 超大响应体日志跳过归一化（直接截原始前缀）
- TCP 写循环：数据顺序落盘；TcpClose 到达时存量先发完；队满只杀该连接
"""

import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import update as sql_update

from tunely.config import TunnelServerConfig
from tunely.models import TunnelRequestLog
from tunely.repository import TunnelRepository, TunnelRequestLogRepository
from tunely.server import TunnelManager, TunnelServer

from .test_server_p1_hardening import _make_responder, _register_conn, _wait_until


@pytest.fixture
async def server():
    srv = TunnelServer(
        config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
    )
    await srv.initialize()
    yield srv
    await srv.close()


# ============== 请求计数批量落库 ==============


class TestRequestCounterBatching:
    @pytest.mark.asyncio
    async def test_forward_does_not_write_db_counter(self, server):
        """转发后 DB 行未写（热路径零 DB 写），内存增量与 live 展示同步"""
        async with server.db.session() as session:
            await TunnelRepository(session).create(domain="cnt", token="tok-cnt")

        ws = _make_responder(server)
        _register_conn(server, "cnt", token="tok-cnt", ws=ws)

        for _ in range(3):
            resp = await server.forward(
                domain="cnt", method="GET", path="/", headers={}
            )
            assert resp.status == 200

        async with server.db.session() as session:
            row = await TunnelRepository(session).get_by_token("tok-cnt")
        assert row.total_requests == 0  # DB 行未写
        assert server._request_counters["cnt"] == 3

        info = await server._get_tunnel("cnt", api_key=None)
        assert info.total_requests == 3  # live 值 = DB + 增量

    @pytest.mark.asyncio
    async def test_flush_persists_and_clears_delta(self, server):
        async with server.db.session() as session:
            await TunnelRepository(session).create(domain="cnt2", token="tok-cnt2")

        ws = _make_responder(server)
        _register_conn(server, "cnt2", token="tok-cnt2", ws=ws)
        await server.forward(domain="cnt2", method="GET", path="/", headers={})

        await server._flush_request_counters()

        async with server.db.session() as session:
            row = await TunnelRepository(session).get_by_token("tok-cnt2")
        assert row.total_requests == 1
        assert server._request_counters.get("cnt2") in (None, 0)

        # live 展示回落为纯 DB 值
        info = await server._get_tunnel("cnt2", api_key=None)
        assert info.total_requests == 1

    @pytest.mark.asyncio
    async def test_flush_db_failure_keeps_delta(self, server):
        """写库失败不丢增量（下轮重试）"""
        server._request_counters["gone"] = 5
        server.db = None  # 模拟 DB 不可用
        await server._flush_request_counters()
        assert server._request_counters["gone"] == 5


# ============== 请求日志后台队列 ==============


class TestRequestLogQueue:
    @pytest.mark.asyncio
    async def test_log_lands_via_background_worker(self, server):
        ws = _make_responder(server)
        _register_conn(server, "bg-dom", ws=ws)
        resp = await server.forward(
            domain="bg-dom", method="GET", path="/x", headers={}
        )
        assert resp.status == 200

        assert await _wait_until(lambda: server._log_queue.empty())
        await asyncio.sleep(0.05)
        async with server.db.session() as session:
            logs = await TunnelRequestLogRepository(session).get_recent(
                tunnel_domain="bg-dom", limit=5
            )
        assert len(logs) == 1

    def test_queue_overflow_drops_and_counts(self, server):
        server._log_queue = asyncio.Queue(maxsize=1)
        server._enqueue_request_log(domain="d", method="GET", path="/1")
        server._enqueue_request_log(domain="d", method="GET", path="/2")
        assert server._log_queue.qsize() == 1
        assert server._request_logs_dropped == 1

    @pytest.mark.asyncio
    async def test_disabled_flag_skips_enqueue(self, server):
        server.config.request_log_enabled = False
        server._enqueue_request_log(domain="d", method="GET", path="/")
        assert server._log_queue.qsize() == 0


# ============== 请求日志保留策略 ==============


class TestRequestLogRetention:
    @pytest.mark.asyncio
    async def test_sweep_deletes_expired_rows_only(self, server):
        server.config.request_log_retention_days = 7
        async with server.db.session() as session:
            log_repo = TunnelRequestLogRepository(session)
            old = await log_repo.create(
                tunnel_domain="r-dom", method="GET", path="/old"
            )
            await log_repo.create(tunnel_domain="r-dom", method="GET", path="/new")

        aged = datetime.now(timezone.utc) - timedelta(days=30)
        async with server.db.session() as session:
            await session.execute(
                sql_update(TunnelRequestLog)
                .where(TunnelRequestLog.id == old.id)
                .values(timestamp=aged)
            )

        deleted = await server._sweep_request_logs()
        assert deleted == 1

        logs = await server._get_tunnel_logs("r-dom", 10, 0, None)
        assert logs["total"] == 1
        assert logs["logs"][0]["path"] == "/new"

    @pytest.mark.asyncio
    async def test_retention_zero_keeps_rows(self, server):
        server.config.request_log_retention_days = 0
        async with server.db.session() as session:
            await TunnelRequestLogRepository(session).create(
                tunnel_domain="r0", method="GET", path="/"
            )
        assert await server._sweep_request_logs() == 0


# ============== HTTP 响应体大小上限 ==============


class TestHttpResponseSizeCap:
    @pytest.mark.asyncio
    async def test_oversized_response_rejected_502(self, server):
        server.config.http_max_response_bytes = 100
        ws = _make_responder(server, body="x" * 500)
        _register_conn(server, "cap-dom", ws=ws)

        resp = await server.forward(
            domain="cap-dom", method="GET", path="/", headers={}
        )
        assert resp.status == 502
        assert "too large" in resp.error

    @pytest.mark.asyncio
    async def test_normal_size_passes_and_unlimited_opt_out(self, server):
        ws = _make_responder(server, body="y" * 500)
        _register_conn(server, "cap-dom2", ws=ws)

        server.config.http_max_response_bytes = 100
        resp = await server.forward(
            domain="cap-dom2", method="GET", path="/", headers={}
        )
        assert resp.status == 502  # 500 字节 > 100 上限

        server.config.http_max_response_bytes = 0  # 0 = 不限制
        resp = await server.forward(
            domain="cap-dom2", method="GET", path="/", headers={}
        )
        assert resp.status == 200


# ============== 超大响应体日志跳过归一化 ==============


class TestHugeBodyLogPrefix:
    @pytest.mark.asyncio
    async def test_huge_body_log_takes_raw_prefix(self, server):
        big_json = json.dumps({"data": "y" * 2_000_000})
        ws = _make_responder(server, body=big_json)
        _register_conn(server, "huge-dom", ws=ws)

        resp = await server.forward(
            domain="huge-dom", method="GET", path="/", headers={}
        )
        assert resp.status == 200

        assert await _wait_until(lambda: server._log_queue.empty())
        await asyncio.sleep(0.05)
        async with server.db.session() as session:
            rows = await TunnelRequestLogRepository(session).get_recent(
                tunnel_domain="huge-dom", limit=5
            )
        assert len(rows) == 1
        assert rows[0].response_body == big_json[:10000]


# ============== TCP 写循环（队头阻塞修复） ==============


class TestTcpWriteLoop:
    @pytest.mark.asyncio
    async def test_data_flows_via_write_loop_in_order(self):
        """WS 侧收到的数据经写循环顺序落到外部 TCP"""
        loop = asyncio.get_running_loop()
        client_sock, target_sock = socket.socketpair()
        client_sock.setblocking(False)
        target_sock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client_sock)
        try:
            manager = TunnelManager()
            await manager.register_tcp_connection(
                "t1", "d", reader, writer, MagicMock()
            )
            assert await manager.handle_tcp_data("t1", b"chunk-1")
            assert await manager.handle_tcp_data("t1", b"chunk-2")

            buf = b""
            while buf != b"chunk-1chunk-2":
                part = await asyncio.wait_for(
                    loop.sock_recv(target_sock, 1024), timeout=2
                )
                if not part:
                    break
                buf += part
            assert buf == b"chunk-1chunk-2"
        finally:
            target_sock.close()
            await manager.close_all_tcp_connections()

    @pytest.mark.asyncio
    async def test_close_flushes_queued_data_before_shutdown(self):
        """TcpClose 到达时，写队列存量数据先发完再关连接（reaper 收尾）"""
        loop = asyncio.get_running_loop()
        client_sock, target_sock = socket.socketpair()
        client_sock.setblocking(False)
        target_sock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=client_sock)
        manager = TunnelManager()
        try:
            await manager.register_tcp_connection(
                "t2", "d", reader, writer, MagicMock()
            )
            await manager.handle_tcp_data("t2", b"pending-a")
            await manager.handle_tcp_data("t2", b"pending-b")
            # 立即移除：存量数据必须仍被发完
            await manager.remove_tcp_connection("t2")

            buf = b""
            while buf != b"pending-apending-b":
                part = await asyncio.wait_for(
                    loop.sock_recv(target_sock, 1024), timeout=2
                )
                if not part:
                    break
                buf += part
            assert buf == b"pending-apending-b"
        finally:
            target_sock.close()
            # 等 reaper 收尾任务结束，避免悬挂任务
            if manager._reapers:
                await asyncio.gather(*list(manager._reapers), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_queue_full_closes_only_that_connection(self):
        """写队列满：该 TCP 连接被移除，不阻塞调用方（WS 消息循环）"""
        manager = TunnelManager()
        ws = MagicMock()
        reader = asyncio.StreamReader()
        writer = MagicMock()
        writer.drain = AsyncMock()
        await manager.register_tcp_connection("t3", "d", reader, writer, ws)
        tcp_conn = await manager.get_tcp_connection("t3")

        # 直接填满写队列（模拟下游消费不动）
        for _ in range(tcp_conn.write_queue.maxsize):
            tcp_conn.write_queue.put_nowait(b"x")

        ok = await manager.handle_tcp_data("t3", b"overflow")
        assert ok is False
        assert await manager.get_tcp_connection("t3") is None  # 只杀该连接

        # 清理写循环任务
        if tcp_conn.write_task:
            tcp_conn.write_task.cancel()
            await asyncio.gather(tcp_conn.write_task, return_exceptions=True)
