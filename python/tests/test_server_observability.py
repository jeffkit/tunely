"""
可观测性 + 持久化 + 审计测试（0.6.0）

- /metrics Prometheus 文本格式端点（可解析、含关键指标行、无鉴权）
- 流量统计持久化（flush 增量累加写库、重启 seed 恢复、DB 故障静默跳过）
- 管理面审计日志（CRUD 埋点 + /api/audit 查询 + WS force takeover 埋点）
"""

import asyncio
import json
import re

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from tunely.config import TunnelServerConfig
from tunely.protocol import AuthMessage
from tunely.repository import TunnelRepository
from tunely.server import CreateTunnelRequest, TcpConnectionState, TunnelServer, UpdateTunnelRequest

# Prometheus 样例行：name{labels} value
_SAMPLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\{[^}]*\})? [0-9.eE+-]+$")


def _make_app(srv: TunnelServer) -> FastAPI:
    app = FastAPI()
    app.include_router(srv.router)
    return app


# ============== /metrics 端点 ==============


class TestMetricsEndpoint:
    @pytest.fixture
    async def metrics_server(self):
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        await srv.initialize()
        yield srv
        await srv.close()

    def test_metrics_content_type_and_parseable(self, metrics_server: TunnelServer):
        client = TestClient(_make_app(metrics_server))
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "version=0.0.4" in resp.headers["content-type"]

        # 每个非注释行都必须是合法的 Prometheus 样例行
        for line in resp.text.splitlines():
            assert line.startswith("#") or _SAMPLE_RE.match(line), (
                f"非法 metrics 行: {line!r}"
            )

    @pytest.mark.asyncio
    async def test_metrics_contains_key_series(self, metrics_server: TunnelServer):
        srv = metrics_server
        await srv._create_tunnel(CreateTunnelRequest(domain="m-dom"), api_key=None)
        srv._count_tunnel_bytes("m-dom", "bytes_in", 100)
        srv._count_tunnel_bytes("m-dom", "bytes_out", 200)
        # 注入一条活跃外部 TCP 连接
        srv.manager._tcp_connections["c1"] = TcpConnectionState(
            conn_id="c1", domain="m-dom", reader=MagicMock(), writer=MagicMock()
        )

        body = TestClient(_make_app(srv)).get("/metrics").text

        assert "# TYPE tunely_tunnels_registered gauge" in body
        assert "tunely_tunnels_registered 1" in body
        assert "tunely_tunnels_connected 0" in body
        assert 'tunely_tcp_connections_active{domain="m-dom"} 1' in body
        assert '# TYPE tunely_tunnel_bytes_in counter' in body
        assert 'tunely_tunnel_bytes_in{domain="m-dom"} 100' in body
        assert 'tunely_tunnel_bytes_out{domain="m-dom"} 200' in body

    @pytest.mark.asyncio
    async def test_metrics_bytes_only_for_domains_with_data(
        self, metrics_server: TunnelServer
    ):
        srv = metrics_server
        await srv._create_tunnel(CreateTunnelRequest(domain="no-traffic"), api_key=None)
        srv._count_tunnel_bytes("busy", "bytes_in", 5)

        body = TestClient(_make_app(srv)).get("/metrics").text

        assert 'tunely_tunnel_bytes_in{domain="busy"} 5' in body
        assert 'domain="no-traffic"' not in body  # 无数据域名不输出 bytes 序列

    def test_metrics_no_auth_required(self, metrics_server: TunnelServer):
        client = TestClient(_make_app(metrics_server))
        assert client.get("/metrics").status_code == 200  # 不带任何凭证

    @pytest.mark.asyncio
    async def test_metrics_degrades_when_db_read_fails(self, metrics_server: TunnelServer):
        srv = metrics_server
        original_db = srv.db

        mock_db = MagicMock()
        mock_db.session = MagicMock(side_effect=RuntimeError("db down"))
        srv.db = mock_db

        try:
            body = TestClient(_make_app(srv)).get("/metrics").text
            assert "tunely_tunnels_registered 0" in body  # 降级为 0 而非 500
        finally:
            srv.db = original_db  # 还原，保证 fixture teardown 正常


# ============== 流量统计持久化 ==============


class TestBytesPersistence:
    @pytest.mark.asyncio
    async def test_flush_accumulates_delta_into_db_row(self, tmp_path):
        url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
        srv = TunnelServer(config=TunnelServerConfig(database_url=url))
        await srv.initialize()
        try:
            await srv._create_tunnel(CreateTunnelRequest(domain="p-dom"), api_key=None)

            srv._count_tunnel_bytes("p-dom", "bytes_in", 111)
            srv._count_tunnel_bytes("p-dom", "bytes_out", 222)
            await srv._flush_tunnel_bytes()  # 手动触发

            async with srv.db.session() as session:
                repo = TunnelRepository(session)
                t = await repo.get_by_domain("p-dom")
                assert t.bytes_in == 111
                assert t.bytes_out == 222

            # 继续计数后再次 flush：只写增量
            srv._count_tunnel_bytes("p-dom", "bytes_in", 5)
            await srv._flush_tunnel_bytes()

            async with srv.db.session() as session:
                repo = TunnelRepository(session)
                t = await repo.get_by_domain("p-dom")
                assert t.bytes_in == 116
                assert t.bytes_out == 222
        finally:
            await srv.close()

    @pytest.mark.asyncio
    async def test_restart_seeds_live_stats_from_db(self, tmp_path):
        url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
        srv = TunnelServer(config=TunnelServerConfig(database_url=url))
        await srv.initialize()
        await srv._create_tunnel(CreateTunnelRequest(domain="p-dom"), api_key=None)
        srv._count_tunnel_bytes("p-dom", "bytes_in", 116)
        srv._count_tunnel_bytes("p-dom", "bytes_out", 9)
        await srv._flush_tunnel_bytes()
        await srv.close()  # close 内含最后落库 + 任务取消

        # 「重启」：新实例从 DB 行恢复内存计数
        srv2 = TunnelServer(config=TunnelServerConfig(database_url=url))
        await srv2.initialize()
        try:
            assert srv2._tunnel_bytes["p-dom"]["bytes_in"] == 116
            assert srv2._tunnel_bytes["p-dom"]["bytes_out"] == 9
            # TunnelInfo 的 live 值跨重启连续
            info = await srv2._get_tunnel("p-dom", api_key=None)
            assert info.bytes_in == 116
            assert info.bytes_out == 9
        finally:
            await srv2.close()

    @pytest.mark.asyncio
    async def test_flush_task_cancelled_on_close(self):
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        await srv.initialize()
        assert srv._bytes_flush_task is not None
        assert not srv._bytes_flush_task.done()
        await srv.close()
        assert srv._bytes_flush_task is None or srv._bytes_flush_task.cancelled()

    @pytest.mark.asyncio
    async def test_flush_loop_survives_db_errors(self, monkeypatch: pytest.MonkeyPatch):
        """DB 不可用时静默跳过：循环不崩溃、不刷屏"""
        import tunely.server as server_module

        monkeypatch.setattr(server_module, "_BYTES_FLUSH_INTERVAL", 0.01)
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        await srv.initialize()

        calls = {"n": 0}

        async def boom() -> None:
            calls["n"] += 1
            raise RuntimeError("db down")

        srv._flush_tunnel_bytes = boom  # type: ignore[method-assign]

        task = asyncio.create_task(srv._bytes_flush_loop())
        try:
            await asyncio.sleep(0.08)
            assert calls["n"] >= 2  # 多轮尝试
            assert not task.done()  # 循环仍然存活（异常被吞掉）
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            srv._bytes_flush_task = None
            await srv.close()

    @pytest.mark.asyncio
    async def test_flush_skips_zero_delta_rows(self, tmp_path):
        """无增量的域名不产生 UPDATE"""
        url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
        srv = TunnelServer(config=TunnelServerConfig(database_url=url))
        await srv.initialize()
        try:
            await srv._create_tunnel(CreateTunnelRequest(domain="idle-dom"), api_key=None)
            assert await srv._flush_tunnel_bytes() is None  # 无增量也不报错
            # 快照推进：flush 后再 flush 不写任何行
            srv._count_tunnel_bytes("idle-dom", "bytes_in", 7)
            await srv._flush_tunnel_bytes()
            async with srv.db.session() as session:
                repo = TunnelRepository(session)
                t = await repo.get_by_domain("idle-dom")
                assert t.bytes_in == 7
            await srv._flush_tunnel_bytes()  # 无增量
            async with srv.db.session() as session:
                repo = TunnelRepository(session)
                t = await repo.get_by_domain("idle-dom")
                assert t.bytes_in == 7  # 值不变
        finally:
            await srv.close()


# ============== 管理面审计日志 ==============


class TestAdminAuditLog:
    @pytest.fixture
    async def audit_server(self):
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                admin_api_key="k",
            )
        )
        await srv.initialize()
        yield srv
        await srv.close()

    @pytest.mark.asyncio
    async def test_crud_actions_recorded_in_reverse_order(
        self, audit_server: TunnelServer
    ):
        srv = audit_server
        await srv._create_tunnel(
            CreateTunnelRequest(domain="a-dom", mode="tcp"), api_key="k"
        )
        await srv._update_tunnel(
            "a-dom", UpdateTunnelRequest(enabled=False, name="renamed"), api_key="k"
        )
        await srv._regenerate_token("a-dom", api_key="k")
        await srv._delete_tunnel("a-dom", api_key="k")

        logs = await srv._get_audit_logs(50, api_key="k")
        actions = [log["action"] for log in logs]
        assert actions == ["delete", "regenerate", "update", "create"]  # 倒序

        update_log = next(l for l in logs if l["action"] == "update")
        assert update_log["detail"] == "changed:enabled,name"
        create_log = next(l for l in logs if l["action"] == "create")
        assert create_log["domain"] == "a-dom"
        assert create_log["detail"] == "mode=tcp"

    @pytest.mark.asyncio
    async def test_audit_api_requires_and_respects_key(
        self, audit_server: TunnelServer
    ):
        client = TestClient(_make_app(audit_server))
        assert client.get("/api/audit").status_code == 401

        resp = client.get("/api/audit?limit=10", headers={"x-api-key": "k"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_route_level_create_records_source_ip(
        self, audit_server: TunnelServer
    ):
        """经真实路由创建隧道时，source_ip 取自 Request.client.host"""
        client = TestClient(_make_app(audit_server))
        resp = client.post(
            "/api/tunnels",
            json={"domain": "route-dom"},
            headers={"x-api-key": "k"},
        )
        assert resp.status_code == 200

        logs = await audit_server._get_audit_logs(50, api_key="k")
        create_log = next(l for l in logs if l["action"] == "create")
        assert create_log["domain"] == "route-dom"
        assert create_log["source_ip"] == "testclient"  # TestClient 的默认 client host

    @pytest.mark.asyncio
    async def test_force_takeover_recorded(self):
        """WS force 抢占成功 → takeover 审计"""
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )

        # mock DB：token 有效
        tunnel = MagicMock()
        tunnel.enabled = True
        tunnel.id = 1
        tunnel.domain = "tk-dom"
        mock_repo = AsyncMock()
        mock_repo.get_by_token = AsyncMock(return_value=tunnel)
        mock_repo.update_last_connected = AsyncMock()
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_db = MagicMock()
        mock_db.session = MagicMock(return_value=mock_session)
        srv.db = mock_db

        # 已有同 token 连接 + force=True
        srv.manager.get_connection_by_token = MagicMock(return_value=MagicMock())
        srv.manager.register = AsyncMock(return_value=(True, None))
        srv.manager.unregister = AsyncMock()

        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.receive_text = AsyncMock(
            side_effect=[
                AuthMessage(token="t-tk", force=True).model_dump_json(),
                WebSocketDisconnect(),
            ]
        )

        audit_mock = AsyncMock()
        with (
            patch("tunely.server.TunnelRepository", return_value=mock_repo),
            patch.object(srv, "_record_audit", audit_mock),
        ):
            await srv._handle_websocket(mock_ws)

        audit_mock.assert_awaited_once()
        args, kwargs = audit_mock.await_args
        assert args == ("takeover",)  # action 以位置参数传入
        assert kwargs["domain"] == "tk-dom"
        assert "takeover" in kwargs["detail"]

    @pytest.mark.asyncio
    async def test_no_takeover_audit_without_existing_connection(self):
        """非 force 或无已有连接时不记 takeover"""
        srv = TunnelServer(
            config=TunnelServerConfig(database_url="sqlite+aiosqlite:///:memory:")
        )
        tunnel = MagicMock()
        tunnel.enabled = True
        tunnel.id = 1
        tunnel.domain = "tk-dom2"
        mock_repo = AsyncMock()
        mock_repo.get_by_token = AsyncMock(return_value=tunnel)
        mock_repo.update_last_connected = AsyncMock()
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_db = MagicMock()
        mock_db.session = MagicMock(return_value=mock_session)
        srv.db = mock_db

        srv.manager.get_connection_by_token = MagicMock(return_value=None)  # 无已有连接
        srv.manager.register = AsyncMock(return_value=(True, None))
        srv.manager.unregister = AsyncMock()

        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.receive_text = AsyncMock(
            side_effect=[
                AuthMessage(token="t-tk2", force=True).model_dump_json(),
                WebSocketDisconnect(),
            ]
        )

        audit_mock = AsyncMock()
        with (
            patch("tunely.server.TunnelRepository", return_value=mock_repo),
            patch.object(srv, "_record_audit", audit_mock),
        ):
            await srv._handle_websocket(mock_ws)

        audit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_action_is_audited_after_commit():
    """回归：delete 审计必须在会话提交后写入，否则 SQLite 锁导致静默丢失"""
    srv = TunnelServer(
        config=TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            admin_api_key="k",
        )
    )
    await srv.initialize()
    try:
        await srv._create_tunnel(CreateTunnelRequest(domain="del-audit"), api_key="k")
        await srv._delete_tunnel("del-audit", api_key="k")
        logs = await srv._get_audit_logs(limit=10, api_key="k")
        assert "delete" in [x["action"] for x in logs]
    finally:
        await srv.close()
