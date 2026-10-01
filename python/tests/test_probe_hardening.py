"""
探测面去特征化测试（docs/PROBE_HARDENING.md）

- T1: /docs /redoc /openapi.json 关闭（404），主动探测拿不到框架与全路由表
- T2: 主域名 GET / 不回 service/version/domain；默认极简 "OK"，decoy 落地页可配
- T3: /health 只回 {"status":"ok"}，不泄露 connected_tunnels 活动 oracle
- T4: /api/info 与 /metrics 受 admin key 门控（与既有 admin 端点同语义：
      配置了 key 则无/错 key 401，未配置 key 放行——生产部署务必配置 key）

T5（认证失败统一时序）与 T6（server_version 可关）分别在
test_server_hardening.py::TestAuthFailureDelay 与 test_server_072_version.py 覆盖。
"""

import pytest
from fastapi.testclient import TestClient

import tunely
from tunely.app import create_full_app

MEM_DB = "sqlite+aiosqlite:///:memory:"


@pytest.fixture()
def client():
    app = create_full_app(domain="probe.test", database_url=MEM_DB)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def keyed_client():
    app = create_full_app(
        domain="probe.test", database_url=MEM_DB, admin_api_key="k-probe"
    )
    with TestClient(app) as c:
        yield c


class TestT1DocsClosed:
    def test_docs_endpoints_404(self, client):
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path


class TestT2RootGeneric:
    def test_root_generic_ok(self, client):
        resp = client.get("/", headers={"host": "probe.test"})
        assert resp.status_code == 200
        body = resp.text.lower()
        # 不再自报家门：服务名/版本/域名一概不出现在根路径响应里
        assert resp.text == "OK"
        assert "tunely" not in body
        assert "version" not in body
        assert "domain" not in body

    def test_root_decoy_file(self, monkeypatch, tmp_path):
        """TUNELY_ROOT_RESPONSE_FILE 配置后返回 decoy 落地页内容"""
        decoy = tmp_path / "decoy.html"
        decoy.write_text(
            "<html><body>Nothing to see here</body></html>", encoding="utf-8"
        )
        monkeypatch.setenv("TUNELY_ROOT_RESPONSE_FILE", str(decoy))
        app = create_full_app(domain="probe.test", database_url=MEM_DB)
        with TestClient(app) as c:
            resp = c.get("/", headers={"host": "probe.test"})
        assert resp.status_code == 200
        assert "Nothing to see here" in resp.text
        assert "tunely" not in resp.text.lower()


class TestT3HealthGeneric:
    def test_health_ok_only(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestT4AdminGated:
    def test_api_info_gated(self, keyed_client):
        assert keyed_client.get("/api/info").status_code == 401
        ok = keyed_client.get("/api/info", headers={"x-api-key": "k-probe"})
        assert ok.status_code == 200
        # /api/info 是运维查询面，不受 T6 expose_version 开关影响，恒回真实版本
        assert ok.json()["version"] == tunely.__version__

    def test_metrics_gated(self, keyed_client):
        assert keyed_client.get("/metrics").status_code == 401
        ok = keyed_client.get("/metrics", headers={"x-api-key": "k-probe"})
        assert ok.status_code == 200
        assert "tunnel" in ok.text

    def test_unconfigured_key_stays_open(self, client):
        # 与既有 admin 端点语义一致：未配置 admin_api_key 时放行（部署须配置 key）
        assert client.get("/api/info").status_code == 200
