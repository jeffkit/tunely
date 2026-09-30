"""/forward 端点鉴权负面安全测试（0.11.1）

背景：该端点曾是无鉴权面（review P0）——在 mode=tcp 隧道上它等价于对
目标服务的裸 TCP 管道，一旦边缘配置失误暴露公网即成开放代理。

本文件锚定鉴权行为（HTTP 层，经真实路由而非直调方法）：
- admin key 已配置：无 key / 错 key → 401；对 key → 通过鉴权进入转发逻辑
- admin key 未配置（内网模式）：无 key 放行（向后兼容，语义与 create/list 一致）
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tunely.config import TunnelServerConfig
from tunely.server import TunnelServer

FORWARD_URL = "/api/tunnels/nonexistent/forward"  # 域名不存在：鉴权通过后应 503 而非 401
BODY = {"method": "GET", "path": "/"}


def _client(admin_api_key: str | None) -> TestClient:
    server = TunnelServer(
        config=TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            admin_api_key=admin_api_key,
        )
    )
    app = FastAPI()
    app.include_router(server.router)
    return TestClient(app, raise_server_exceptions=False)


class TestForwardRequiresApiKey:
    """admin key 已配置（生产姿态）：无/错 key 必须 401"""

    def test_missing_key_rejected(self):
        resp = _client("secret-key").post(FORWARD_URL, json=BODY)
        assert resp.status_code == 401

    def test_wrong_key_rejected(self):
        resp = _client("secret-key").post(
            FORWARD_URL, json=BODY, headers={"x-api-key": "wrong"})
        assert resp.status_code == 401

    def test_correct_key_passes_auth(self):
        """对 key → 401 消失；body.status=503（隧道未连接）证明已进入转发逻辑。

        注意该端点 HTTP 状态恒为 200（response_model=ForwardResponse），
        转发结果在 body 的 status/error 字段承载。
        """
        resp = _client("secret-key").post(
            FORWARD_URL, json=BODY, headers={"x-api-key": "secret-key"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == 503
        assert "Tunnel not connected" in data["error"]

    def test_auth_precedes_ssrf_path_check(self):
        """鉴权先于 path 校验：无 key 时即便非法 path 也是 401 而非 400"""
        resp = _client("secret-key").post(
            FORWARD_URL, json={"method": "GET", "path": "@169.254.169.254/"})
        assert resp.status_code == 401


class TestForwardInternalModeUnchanged:
    """admin key 未配置（内网模式）：无 key 放行，行为不变"""

    def test_no_key_configured_allows_forward(self):
        resp = _client(None).post(FORWARD_URL, json=BODY)
        assert resp.status_code == 200
        assert resp.json()["status"] == 503
