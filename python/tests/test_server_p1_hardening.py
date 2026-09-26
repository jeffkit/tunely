"""
服务端 P1 批次加固测试（0.7.0）

- 6. L1 建隧道校验 domain（非法格式 400，合法域名兼容）
- 7. L2 请求日志脱敏（authorization/cookie/set-cookie → [REDACTED]）
     与响应 JSON body 双解析路径 10000 字符截断
- 8. F7 SQLite 并发写：WAL + connect timeout 30s；请求计数与日志写拆独立事务
- 10. F22 端口冲突友好报错（列出 host:port 与归属 domain）
- 11. uvloop 可选加速（缺省回退默认事件循环）
"""

import asyncio
import json
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import tunely.app
from tunely.app import create_full_app
from tunely.config import TunnelServerConfig
from tunely.database import DatabaseManager
from tunely.protocol import TunnelResponse
from tunely.repository import TunnelRepository, TunnelRequestLogRepository
from tunely.server import (
    ActiveConnection,
    CreateTunnelRequest,
    TunnelServer,
)

# ============== 测试辅助 ==============


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


def _register_conn(server: TunnelServer, domain: str, token: str | None = None, ws=None):
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


# ============== 6. L1 建隧道校验 domain ==============


class TestCreateTunnelDomainValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_domain",
        [
            "has space",       # 空格
            "中文域",           # 中文
            "a@b",             # @
            "x" * 64,          # 超长（>63）
            "",                # 空
            "-lead-dash",      # 中划线开头
            "do.main",         # 点号
        ],
    )
    async def test_invalid_domain_rejected_400(self, server, bad_domain):
        with pytest.raises(HTTPException) as exc_info:
            await server._create_tunnel(
                CreateTunnelRequest(domain=bad_domain), api_key=None
            )
        assert exc_info.value.status_code == 400
        assert "Invalid domain" in exc_info.value.detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize("valid_domain", ["dsh", "smoke-dom", "a", "x" * 63, "A9"])
    async def test_valid_domains_still_accepted(self, server, valid_domain):
        """既有合法域名（dsh / smoke-dom 等）不受影响"""
        resp = await server._create_tunnel(
            CreateTunnelRequest(domain=valid_domain), api_key=None
        )
        assert resp.domain == valid_domain

    def test_route_level_invalid_domain_returns_400(self):
        """经真实 HTTP 路由同样 400"""
        app = create_full_app(database_url="sqlite+aiosqlite:///:memory:")
        with TestClient(app) as client:
            resp = client.post("/api/tunnels", json={"domain": "bad domain"})
            assert resp.status_code == 400

            ok = client.post("/api/tunnels", json={"domain": "route-ok"})
            assert ok.status_code == 200


# ============== 7. L2 请求日志脱敏与截断 ==============


class TestRequestLogRedaction:
    def test_redact_headers_case_insensitive(self):
        out = TunnelServer._redact_headers_for_log(
            {
                "Authorization": "Bearer secret-token",
                "cookie": "session=xyz",
                "SET-COOKIE": "a=b; HttpOnly",
                "Content-Type": "application/json",
                "X-Custom": "keep-me",
            }
        )
        assert out["Authorization"] == "[REDACTED]"
        assert out["cookie"] == "[REDACTED]"
        assert out["SET-COOKIE"] == "[REDACTED]"
        assert out["Content-Type"] == "application/json"
        assert out["X-Custom"] == "keep-me"

    def test_redact_none_passthrough(self):
        assert TunnelServer._redact_headers_for_log(None) is None
        assert TunnelServer._redact_headers_for_log({}) == {}

    @pytest.mark.asyncio
    async def test_forward_logs_redacted_and_truncated(self, server):
        big_json = json.dumps({"data": "x" * 20000})
        ws = _make_responder(
            server,
            headers={"Set-Cookie": "sid=secret-cookie"},
            body=big_json,
        )
        _register_conn(server, "log-dom", ws=ws)

        resp = await server.forward(
            domain="log-dom",
            method="GET",
            path="/x",
            headers={
                "Authorization": "Bearer super-secret",
                "Cookie": "k=v; session=leak",
                "X-Ok": "1",
            },
        )
        assert resp.status == 200

        # 直查模型行（绕过 to_dict 的 500 字符展示截断）
        async with server.db.session() as session:
            log_repo = TunnelRequestLogRepository(session)
            rows = await log_repo.get_recent(tunnel_domain="log-dom", limit=10)
        assert len(rows) == 1
        row = rows[0]

        stored_req_headers = json.loads(row.request_headers)
        assert stored_req_headers["Authorization"] == "[REDACTED]"
        assert stored_req_headers["Cookie"] == "[REDACTED]"
        assert "super-secret" not in row.request_headers
        assert stored_req_headers["X-Ok"] == "1"

        stored_resp_headers = json.loads(row.response_headers)
        assert stored_resp_headers["Set-Cookie"] == "[REDACTED]"

        # 双解析路径截断：归一化 JSON 被截到 10000 字符
        assert row.response_body is not None
        assert len(row.response_body) == 10000
        assert row.response_body.startswith('{"data": "xxx')


# ============== 8. F7 SQLite 并发写 ==============


class TestSqliteConcurrencyTuning:
    @pytest.mark.asyncio
    async def test_wal_enabled_on_file_db(self, tmp_path: Path):
        db_path = tmp_path / "wal-check.db"
        mgr = DatabaseManager(f"sqlite+aiosqlite:///{db_path}")
        await mgr.initialize()
        try:
            async with mgr._engine.connect() as conn:
                mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
            assert str(mode).lower() == "wal"
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_connect_args_timeout_30(self, tmp_path: Path):
        mgr = DatabaseManager(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await mgr.initialize()
        try:
            assert mgr._connect_args == {"timeout": 30}
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_memory_db_still_works(self, db_manager: DatabaseManager):
        """:memory: 库在 WAL 监听器下不炸（journal_mode 对内存库为 memory）"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            await repo.create(domain="mem-ok")
            row = await repo.get_by_domain("mem-ok")
            assert row is not None

    @pytest.mark.asyncio
    async def test_log_failure_does_not_rollback_request_counter(self, server):
        """请求计数与日志写拆独立事务：日志失败不回滚计数"""
        async with server.db.session() as session:
            repo = TunnelRepository(session)
            await repo.create(domain="cnt-dom", token="tok-cnt-dom")

        ws = _make_responder(server)
        _register_conn(server, "cnt-dom", token="tok-cnt-dom", ws=ws)

        with patch.object(
            TunnelRequestLogRepository,
            "create",
            AsyncMock(side_effect=RuntimeError("log boom")),
        ):
            resp = await server.forward(
                domain="cnt-dom", method="GET", path="/", headers={}
            )
        assert resp.status == 200

        async with server.db.session() as session:
            repo = TunnelRepository(session)
            row = await repo.get_by_token("tok-cnt-dom")
        assert row is not None
        assert row.total_requests == 1  # 计数已提交

        logs = await server._get_tunnel_logs("cnt-dom", 10, 0, None)
        assert logs["total"] == 0  # 日志确实失败


# ============== 10. F22 端口冲突友好报错 ==============


class TestPortConflictFriendlyError:
    @pytest.mark.asyncio
    async def test_eaddrinuse_lists_host_port_and_domain(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            srv = TunnelServer(
                config=TunnelServerConfig(
                    database_url="sqlite+aiosqlite:///:memory:",
                    tcp_listen=f"{port}:conf-dom",
                )
            )
            with pytest.raises(RuntimeError) as exc_info:
                await srv._start_tcp_listeners()
            msg = str(exc_info.value)
            assert f"127.0.0.1:{port}" in msg
            assert "conf-dom" in msg
            assert "TCP 监听端口被占用" in msg
        finally:
            blocker.close()

    @pytest.mark.asyncio
    async def test_other_os_errors_still_propagate(self):
        """非 EADDRINUSE 的 OSError 原样抛出（不吞）"""
        srv = TunnelServer(
            config=TunnelServerConfig(
                database_url="sqlite+aiosqlite:///:memory:",
                tcp_listen="1:bad-dom",  # 端口 1 绑定需要特权 → EACCES
            )
        )
        with pytest.raises(OSError):
            await srv._start_tcp_listeners()


# ============== 11. uvloop 可选加速 ==============


class TestUvloopOptional:
    def test_resolver_returns_valid_choice(self):
        # 装了 uvloop → "uvloop"；没装 → "auto"；都不抛异常
        assert tunely.app._uvicorn_loop() in {"uvloop", "auto"}

    def test_run_app_passes_loop_to_uvicorn(self):
        with patch("uvicorn.run") as fake_run, patch.object(
            tunely.app, "create_full_app"
        ):
            tunely.app.run_app(host="127.0.0.1", port=59999)
        assert fake_run.call_args.kwargs["loop"] in {"uvloop", "auto"}

    def test_uvloop_extra_declared(self):
        import tomllib

        pyproject = (
            Path(__file__).resolve().parent.parent / "pyproject.toml"
        )
        data = tomllib.loads(pyproject.read_text())
        extras = data["project"]["optional-dependencies"]
        assert "uvloop" in extras
        assert any(dep.startswith("uvloop") for dep in extras["uvloop"])
