"""
多租户自助控制台后端测试（docs/CONSOLE_MULTITENANT.md 契约 §8 后端验收）

覆盖：
- 邀请码三态（有效/过期/用尽）+ 无效码、用户名冲突、用户名/密码格式
- 注册 / 登录（对/错/延迟）/ 登出 / me（含禁用 403）
- 隧道自助 CRUD：token 仅 create/rotate 可见、配额 409（含 0=禁建）、域名冲突 409
- 所有权：越权一律 404（不泄露存在性）
- rotate-token 踢存量连接、旧 token 失效；delete 断开在线连接
- admin key 通道回归不受影响 + admin/users、admin/invites
- 会话 cookie 属性（HttpOnly / SameSite=Lax / Path=/ / Max-Age 7d）与签名校验
"""

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from tunely.config import TunnelServerConfig
from tunely.console_api import (
    ConsoleError,
    ConsoleSessionManager,
    InviteRepository,
    UserRepository,
    console_error_handler,
    hash_password,
    mount_console_api,
    resolve_session_secret,
    verify_password,
)
from tunely.database import DatabaseManager
from tunely.models import Invite
from tunely.repository import TunnelRepository
from tunely.server import TunnelServer

ADMIN_KEY = "test-admin-key"
SESSION_SECRET = "unit-test-session-secret"
ADMIN_HEADERS = {"x-api-key": ADMIN_KEY}


# ============== 环境搭建 ==============


async def _make_console_env(**config_overrides) -> tuple[TunnelServer, httpx.AsyncClient]:
    """构造内存库 TunnelServer + 挂载控制台路由的 httpx 客户端

    不走 server.initialize()（避免后台 flush/log 任务），直接注入 db——
    控制台端点只在请求时访问 server.db / manager，与生产行为一致。
    """
    config = TunnelServerConfig(
        database_url="sqlite+aiosqlite:///:memory:",
        domain="tunely.example.com",
        admin_api_key=ADMIN_KEY,
        console_session_secret=SESSION_SECRET,
        console_login_failure_delay=0.25,  # 契约默认 1s，测试收紧但保持可断言
        console_tunnels_per_user=2,
    )
    # 覆盖项后置更新（避免与默认值重复传参冲突）
    for key, value in config_overrides.items():
        setattr(config, key, value)
    server = TunnelServer(config)
    server.db = DatabaseManager(config.database_url)
    await server.db.initialize()

    # 与生产 create_full_app 同形：admin 通道（server.router）+ 控制台路由并存
    app = FastAPI()
    app.include_router(server.router)
    mount_console_api(app, server)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    return server, client


async def _dispose(server: TunnelServer, client: httpx.AsyncClient) -> None:
    await client.aclose()
    await server.db.close()


@pytest.fixture
async def env():
    server, client = await _make_console_env()
    yield server, client
    await _dispose(server, client)


# ============== 操作助手 ==============


async def make_invite(
    client: httpx.AsyncClient, max_uses: int = 1, role: str = "tenant"
) -> str:
    resp = await client.post(
        "/api/console/admin/invites",
        json={"max_uses": max_uses, "role": role},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["code"]


async def register_tenant(
    client: httpx.AsyncClient,
    username: str,
    invite_code: str,
    password: str = "password123",
) -> httpx.Response:
    return await client.post(
        "/api/console/register",
        json={"username": username, "password": password, "invite_code": invite_code},
    )


async def register_and_login(
    server: TunnelServer,
    client: httpx.AsyncClient,
    username: str,
    password: str = "password123",
) -> None:
    """签发邀请码 + 注册 + 登录（cookie 落在 client）"""
    code = await make_invite(client)
    resp = await register_tenant(client, username, code, password)
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/api/console/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text


async def add_expired_invite(server: TunnelServer, code: str) -> None:
    async with server.db.session() as session:
        session.add(
            Invite(
                code=code,
                max_uses=5,
                used_count=0,
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(days=1),
            )
        )


def err_code(resp: httpx.Response) -> str:
    body = resp.json()
    assert set(body.keys()) == {"error"}, f"错误形状应为 {{'error': ...}}: {body}"
    assert set(body["error"].keys()) == {"code", "message"}
    return body["error"]["code"]


# ============== 密码哈希（契约 §3） ==============


class TestPasswordHashing:
    def test_hash_format_scrypt_n_r_p_salt_hash(self):
        h = hash_password("s3cret-password")
        parts = h.split("$")
        assert len(parts) == 6
        assert parts[0] == "scrypt"
        assert parts[1] == str(2**14) and parts[2] == "8" and parts[3] == "1"
        assert len(bytes.fromhex(parts[4])) == 16  # 16B 盐
        assert len(bytes.fromhex(parts[5])) == 64

    def test_verify_roundtrip_and_wrong_password(self):
        h = hash_password("right-password")
        assert verify_password("right-password", h) is True
        assert verify_password("wrong-password", h) is False

    def test_verify_malformed_hash_is_false(self):
        assert verify_password("x", "not-a-valid-hash") is False
        assert verify_password("x", "md5$1$2$zz$zz") is False

    def test_salt_is_random_per_hash(self):
        assert hash_password("same") != hash_password("same")


# ============== 会话（契约 §4） ==============


class TestSessionManager:
    def test_issue_and_verify_roundtrip(self):
        mgr = ConsoleSessionManager(b"secret")
        payload = mgr.verify(mgr.issue("alice", "tenant"))
        assert payload is not None
        assert payload["u"] == "alice" and payload["r"] == "tenant"
        assert payload["exp"] > time.time()

    def test_tampered_token_rejected(self):
        mgr = ConsoleSessionManager(b"secret")
        token = mgr.issue("alice", "tenant")
        assert mgr.verify(token + "x") is None
        assert mgr.verify(token[:-4] + "beef") is None

    def test_wrong_secret_rejected(self):
        token = ConsoleSessionManager(b"secret-a").issue("alice", "tenant")
        assert ConsoleSessionManager(b"secret-b").verify(token) is None

    def test_expired_token_rejected(self):
        mgr = ConsoleSessionManager(b"secret")
        token = mgr.issue("alice", "tenant", now=0)
        assert mgr.verify(token) is None

    def test_default_max_age_is_seven_days(self):
        assert ConsoleSessionManager.MAX_AGE_SECONDS == 7 * 24 * 3600

    def test_resolve_secret_priority_and_file_fallback(self, tmp_path):
        config = TunnelServerConfig(console_session_secret="inline")
        assert resolve_session_secret(config) == b"inline"

        secret_file = tmp_path / "console.secret"
        config = TunnelServerConfig(console_session_secret_file=str(secret_file))
        first = resolve_session_secret(config)
        assert secret_file.exists()
        assert resolve_session_secret(config) == first  # 复用已生成文件，重启会话连续

        config = TunnelServerConfig()
        assert len(resolve_session_secret(config)) == 32  # 进程内随机兜底


# ============== 注册与邀请码三态（契约 §5/§8） ==============


@pytest.mark.asyncio
async def test_register_with_valid_invite_creates_tenant(env):
    server, client = env
    code = await make_invite(client, max_uses=2)
    resp = await register_tenant(client, "alice", code)
    assert resp.status_code == 201
    assert resp.json() == {"username": "alice", "role": "tenant"}

    async with server.db.session() as session:
        user = await UserRepository(session).get_by_username("alice")
        assert user is not None and user.role == "tenant" and not user.disabled
        invite = await InviteRepository(session).get_by_code(code)
        assert invite.used_count == 1  # 契约 §5：注册成功 used_count+1


@pytest.mark.asyncio
async def test_invite_states_invalid_expired_exhausted(env):
    server, client = env

    # 无效：不存在的码
    resp = await register_tenant(client, "alice", "no-such-code")
    assert resp.status_code == 400
    assert err_code(resp) == "invite_invalid"

    # 过期：直接造一条已过期的邀请码
    await add_expired_invite(server, "stale-code")
    resp = await register_tenant(client, "alice", "stale-code")
    assert resp.status_code == 400
    assert err_code(resp) == "invite_expired"

    # 用尽：max_uses=1 注册第二次
    code = await make_invite(client, max_uses=1)
    assert (await register_tenant(client, "alice", code)).status_code == 201
    resp = await register_tenant(client, "bob", code)
    assert resp.status_code == 400
    assert err_code(resp) == "invite_exhausted"


@pytest.mark.asyncio
async def test_register_rejects_bad_username_password_and_duplicate(env):
    server, client = env
    code = await make_invite(client, max_uses=5)

    resp = await register_tenant(client, "Bad Name!", code)
    assert resp.status_code == 400 and err_code(resp) == "invalid_username"
    resp = await register_tenant(client, "ab", code)  # < 3 位
    assert resp.status_code == 400 and err_code(resp) == "invalid_username"
    resp = await register_tenant(client, "alice", code, password="short")
    assert resp.status_code == 400 and err_code(resp) == "invalid_password"

    assert (await register_tenant(client, "alice", code)).status_code == 201
    code2 = await make_invite(client)
    resp = await register_tenant(client, "alice", code2)
    assert resp.status_code == 409 and err_code(resp) == "username_taken"

    # 失败的注册不消耗邀请额度
    async with server.db.session() as session:
        invite = await InviteRepository(session).get_by_code(code2)
        assert invite.used_count == 0


# ============== 登录 / 登出 / me（契约 §5/§8） ==============


@pytest.mark.asyncio
async def test_login_success_sets_session_cookie(env):
    _, client = env
    code = await make_invite(client)
    await register_tenant(client, "alice", code)

    resp = await client.post(
        "/api/console/login", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"username": "alice", "role": "tenant"}
    set_cookie = resp.headers["set-cookie"]
    assert "console_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=604800" in set_cookie  # 7d


async def register_and_login_client(client: httpx.AsyncClient) -> None:
    code = await make_invite(client)
    await register_tenant(client, "alice", code)
    resp = await client.post(
        "/api/console/login", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_failure_is_delayed(env):
    _, client = env
    code = await make_invite(client)
    await register_tenant(client, "alice", code)

    start = time.monotonic()
    resp = await client.post(
        "/api/console/login", json={"username": "alice", "password": "wrong-password"}
    )
    elapsed = time.monotonic() - start
    assert resp.status_code == 401
    assert err_code(resp) == "invalid_credentials"
    assert elapsed >= 0.25, "登录失败必须触发防爆破延迟"

    # 未知用户同样延迟（不泄露账号存在性）
    start = time.monotonic()
    resp = await client.post(
        "/api/console/login", json={"username": "ghost", "password": "whatever123"}
    )
    assert resp.status_code == 401
    assert time.monotonic() - start >= 0.25


@pytest.mark.asyncio
async def test_me_logout_flow(env):
    _, client = env
    await register_and_login_client(client)

    resp = await client.get("/api/console/me")
    assert resp.status_code == 200
    assert resp.json() == {"username": "alice", "role": "tenant", "disabled": False}

    resp = await client.post("/api/console/logout")
    assert resp.status_code == 204

    resp = await client.get("/api/console/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_console_access_rejected(env):
    _, client = env
    for method, path in [
        ("GET", "/api/console/me"),
        ("GET", "/api/console/tunnels"),
        ("POST", "/api/console/tunnels"),
        ("POST", "/api/console/logout"),
        ("GET", "/api/console/entry"),
    ]:
        resp = await client.request(
            method, path, json={"prefix": "x"} if method == "POST" else None
        )
        assert resp.status_code == 401, f"{method} {path} 应要求会话"
        assert err_code(resp) == "unauthorized"


@pytest.mark.asyncio
async def test_disabled_user_me_403_and_login_401(env):
    server, client = env
    await register_and_login_client(client)

    resp = await client.patch(
        "/api/console/admin/users/alice",
        json={"disabled": True},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200

    # 既有会话：me → 403（契约 §5）
    resp = await client.get("/api/console/me")
    assert resp.status_code == 403 and err_code(resp) == "account_disabled"
    # 隧道操作同样被拦
    resp = await client.get("/api/console/tunnels")
    assert resp.status_code == 403

    # 重新登录被拒（禁用账号按登录失败处理）
    resp = await client.post(
        "/api/console/login", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 401

    # 解禁后原会话恢复可用（无状态会话 + 每请求查库）
    resp = await client.patch(
        "/api/console/admin/users/alice",
        json={"disabled": False},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    resp = await client.get("/api/console/me")
    assert resp.status_code == 200


# ============== 隧道自助管理（契约 §5/§8） ==============


@pytest.mark.asyncio
async def test_create_tunnel_returns_token_once_and_list_has_no_token(env):
    _, client = env
    await register_and_login_client(client)

    resp = await client.post("/api/console/tunnels", json={"prefix": "alice-app"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["domain"] == "alice-app"
    assert body["token"].startswith("tun_")

    # 列表不含 token（契约 §5）
    resp = await client.get("/api/console/tunnels")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert "token" not in item
    assert item["domain"] == "alice-app"
    assert item["online"] is False
    assert item["bytes_in"] == 0 and item["bytes_out"] == 0
    assert item["created_at"] is not None


@pytest.mark.asyncio
async def test_quota_409_and_zero_quota(env):
    _, client = env
    await register_and_login_client(client)

    assert (
        await client.post("/api/console/tunnels", json={"prefix": "q1"})
    ).status_code == 201
    assert (
        await client.post("/api/console/tunnels", json={"prefix": "q2"})
    ).status_code == 201
    resp = await client.post("/api/console/tunnels", json={"prefix": "q3"})
    assert resp.status_code == 409
    assert err_code(resp) == "quota_exceeded"  # 配额 = console_tunnels_per_user（fixture 为 2）

    # 0 = 该租户禁建
    server0, client0 = await _make_console_env(console_tunnels_per_user=0)
    try:
        await register_and_login_client(client0)
        resp = await client0.post("/api/console/tunnels", json={"prefix": "first"})
        assert resp.status_code == 409
        assert err_code(resp) == "quota_exceeded"
    finally:
        await _dispose(server0, client0)


@pytest.mark.asyncio
async def test_domain_conflict_409(env):
    server, client = env
    # admin key 通道先占住前缀
    resp = await client.post(
        "/api/tunnels", json={"domain": "shared"}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200  # admin 通道创建既有语义为 200

    await register_and_login_client(client)
    resp = await client.post("/api/console/tunnels", json={"prefix": "shared"})
    assert resp.status_code == 409
    assert err_code(resp) == "domain_taken"

    # 非法前缀（大小写会被归一化为小写，不在此列）
    for bad in ["-lead", "trail-", "a" * 64, "", "sp ace"]:
        resp = await client.post("/api/console/tunnels", json={"prefix": bad})
        assert resp.status_code == 400, f"prefix={bad!r} 应 400"
        assert err_code(resp) == "invalid_prefix"

    # 大写前缀归一化为小写存储
    resp = await client.post("/api/console/tunnels", json={"prefix": "CamelCase"})
    assert resp.status_code == 201
    assert resp.json()["domain"] == "camelcase"


@pytest.mark.asyncio
async def test_cross_tenant_access_is_404(env):
    _, client = env
    await register_and_login_client(client)  # alice
    resp = await client.post("/api/console/tunnels", json={"prefix": "alice-app"})
    assert resp.status_code == 201

    # bob（另一租户，独立 cookie jar）
    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._transport.app),
        base_url="http://testserver",
    )
    try:
        await register_and_login_tenant(bob, "bob")

        resp = await bob.post("/api/console/tunnels/alice-app/rotate-token")
        assert resp.status_code == 404 and err_code(resp) == "not_found"
        resp = await bob.delete("/api/console/tunnels/alice-app")
        assert resp.status_code == 404 and err_code(resp) == "not_found"

        # bob 的列表看不到 alice 的隧道
        domains = [t["domain"] for t in (await bob.get("/api/console/tunnels")).json()]
        assert "alice-app" not in domains

        # 不存在的隧道同样 404（越权与不存在不可区分）
        resp = await bob.post("/api/console/tunnels/ghost/rotate-token")
        assert resp.status_code == 404
        resp = await bob.delete("/api/console/tunnels/ghost")
        assert resp.status_code == 404
    finally:
        await bob.aclose()

    # alice 自己仍可操作（删除归自己，释放域名）
    resp = await client.delete("/api/console/tunnels/alice-app")
    assert resp.status_code == 204
    resp = await client.post("/api/console/tunnels", json={"prefix": "alice-app"})
    assert resp.status_code == 201


async def register_and_login_tenant(client: httpx.AsyncClient, username: str) -> None:
    code = await make_invite(client)
    await register_tenant(client, username, code)
    resp = await client.post(
        "/api/console/login", json={"username": username, "password": "password123"}
    )
    assert resp.status_code == 200


# ============== rotate-token 踢连接 / delete 断连（契约 §5/§8） ==============


async def _connect_fake(server: TunnelServer, domain: str, token: str) -> AsyncMock:
    """向 TunnelManager 注册一个假连接（模拟在线隧道客户端）"""
    ws = AsyncMock()
    ok, err = await server.manager.register(
        websocket=ws, tunnel_id=1, domain=domain, token=token
    )
    assert ok, err
    return ws


@pytest.mark.asyncio
async def test_rotate_token_kicks_existing_connection(env):
    server, client = env
    await register_and_login_client(client)
    resp = await client.post("/api/console/tunnels", json={"prefix": "app"})
    old_token = resp.json()["token"]

    fake_ws = await _connect_fake(server, "app", old_token)
    assert server.manager.is_connected("app")

    resp = await client.post("/api/console/tunnels/app/rotate-token")
    assert resp.status_code == 201
    body = resp.json()
    assert body["domain"] == "app"
    new_token = body["token"]
    assert new_token != old_token and new_token.startswith("tun_")

    # 旧连接被踢（服务端主动 close，reason=token rotated）
    fake_ws.close.assert_awaited_once()
    assert fake_ws.close.await_args.kwargs.get("reason") == "token rotated"

    # 旧 token 立即失效，新 token 可用（以 manager 注册成功为准）
    async with server.db.session() as session:
        assert await TunnelRepository(session).get_by_token(old_token) is None
        assert (await TunnelRepository(session).get_by_token(new_token)) is not None
    await _connect_fake(server, "app", new_token)
    assert server.manager.get_connection_by_domain("app").token == new_token

    # 列表 online 与 last_seen 正确反映连接状态
    items = (await client.get("/api/console/tunnels")).json()
    assert items[0]["online"] is True
    assert items[0]["last_seen_at"] is None  # 从未真实连接（last_connected_at 未写）


@pytest.mark.asyncio
async def test_delete_tunnel_disconnects_and_frees_domain(env):
    server, client = env
    await register_and_login_client(client)
    resp = await client.post("/api/console/tunnels", json={"prefix": "app"})
    token = resp.json()["token"]

    fake_ws = await _connect_fake(server, "app", token)

    resp = await client.delete("/api/console/tunnels/app")
    assert resp.status_code == 204
    fake_ws.close.assert_awaited_once()
    assert fake_ws.close.await_args.kwargs.get("reason") == "tunnel revoked"
    assert (await client.get("/api/console/tunnels")).json() == []


# ============== admin 通道回归 + 管理端点（契约 §5/§8） ==============


@pytest.mark.asyncio
async def test_admin_key_channel_regression(env):
    server, client = env
    await register_and_login_client(client)
    await client.post("/api/console/tunnels", json={"prefix": "tenant-app"})

    # admin 建隧道（既有行为不变：200 + token 明文）
    resp = await client.post(
        "/api/tunnels", json={"domain": "admin-app"}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200
    admin_token = resp.json()["token"]

    # admin 列表能看到两类隧道；owner 归属正确（admin/遗留 = NULL）
    resp = await client.get("/api/tunnels", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    by_domain = {t["domain"]: t for t in resp.json()}
    assert set(by_domain) == {"admin-app", "tenant-app"}

    async with server.db.session() as session:
        repo = TunnelRepository(session)
        assert (await repo.get_by_domain("admin-app")).owner_id is None
        assert (await repo.get_by_domain("tenant-app")).owner_id is not None

    # admin rotate（既有行为不变：token 明文返回 + 踢连接）
    fake_ws = await _connect_fake(server, "admin-app", admin_token)
    resp = await client.post(
        "/api/tunnels/admin-app/regenerate-token", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["token"] != admin_token
    fake_ws.close.assert_awaited_once()

    # 无 key / 错 key 访问 admin 通道 → 401
    assert (await client.get("/api/tunnels")).status_code == 401
    resp = await client.get("/api/tunnels", headers={"x-api-key": "wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_endpoints_reject_tenant_session_and_accept_admin_session(env):
    """契约 v1.1：管理端点鉴权 = admin key 或 admin 会话（二选一）"""
    _, client = env
    await register_and_login_client(client)  # alice = tenant

    # tenant 会话（无 key）→ 403 forbidden（角色不足，非未认证）
    resp = await client.get("/api/console/admin/users")
    assert resp.status_code == 403 and err_code(resp) == "forbidden"
    resp = await client.post("/api/console/admin/invites", json={})
    assert resp.status_code == 403
    resp = await client.patch("/api/console/admin/users/alice", json={"disabled": False})
    assert resp.status_code == 403

    # 错 key + tenant 会话 → 仍 403（key 无效后走会话路径被角色拦截）
    resp = await client.get(
        "/api/console/admin/users", headers={"x-api-key": "wrong"}
    )
    assert resp.status_code == 403

    # 无会话 + 无 key → 401
    anon = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._transport.app),
        base_url="http://testserver",
    )
    try:
        resp = await anon.get("/api/console/admin/users", headers={"x-api-key": "nope"})
        assert resp.status_code == 401 and err_code(resp) == "unauthorized"
    finally:
        await anon.aclose()

    # admin 邀请码注册的 admin 会话可通过（无需 key）
    admin_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._transport.app),
        base_url="http://testserver",
    )
    try:
        code = await make_invite(admin_client, role="admin")
        await register_tenant(admin_client, "boss", code)
        resp = await admin_client.post(
            "/api/console/login", json={"username": "boss", "password": "password123"}
        )
        assert resp.status_code == 200 and resp.json()["role"] == "admin"

        resp = await admin_client.get("/api/console/admin/users")
        assert resp.status_code == 200
        assert {u["username"] for u in resp.json()} == {"alice", "boss"}
        # admin 会话签发邀请码 ✓
        resp = await admin_client.post("/api/console/admin/invites", json={})
        assert resp.status_code == 200 and "code" in resp.json()
        # admin 会话 PATCH 用户 ✓
        resp = await admin_client.patch(
            "/api/console/admin/users/alice", json={"disabled": False}
        )
        assert resp.status_code == 200
    finally:
        await admin_client.aclose()


@pytest.mark.asyncio
async def test_admin_users_list_shape_with_tunnel_count(env):
    """契约 v1.1 形状：{id, username, role, disabled, created_at, tunnel_count}"""
    server, client = env
    await register_and_login_client(client)
    await client.post("/api/console/tunnels", json={"prefix": "count-app"})

    resp = await client.get("/api/console/admin/users", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) == 1
    row = users[0]
    assert set(row.keys()) == {"id", "username", "role", "disabled", "created_at", "tunnel_count"}
    assert row["username"] == "alice"
    assert row["role"] == "tenant"
    assert row["disabled"] is False
    assert isinstance(row["id"], int) and row["id"] > 0
    assert row["tunnel_count"] == 1

    # admin/遗留隧道（owner NULL）不计入任何用户
    await client.post("/api/tunnels", json={"domain": "orphan"}, headers=ADMIN_HEADERS)
    resp = await client.get("/api/console/admin/users", headers=ADMIN_HEADERS)
    assert resp.json()[0]["tunnel_count"] == 1

    # PATCH 未知用户 → 404
    resp = await client.patch(
        "/api/console/admin/users/ghost",
        json={"disabled": True},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 404 and err_code(resp) == "not_found"

    # PATCH 非法 role / 空 body → 400
    resp = await client.patch(
        "/api/console/admin/users/alice", json={"role": "hacker"}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 400 and err_code(resp) == "invalid_role"
    resp = await client.patch(
        "/api/console/admin/users/alice", json={}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 400 and err_code(resp) == "bad_request"

    # 旧 POST /admin/users 已被 v1.1 的 PATCH 取代（形状收口）
    resp = await client.post(
        "/api/console/admin/users",
        json={"username": "alice", "disabled": True},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_patch_user_role_and_disabled_take_effect_immediately(env):
    """角色/禁用每请求查库 → 即时生效（被降级的 admin 会话下一个请求即 403）"""
    server, client = env
    # alice = admin 会话
    code = await make_invite(client, role="admin")
    await register_tenant(client, "alice", code)
    resp = await client.post(
        "/api/console/login", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 200

    # 用 admin key 把 bob 从 tenant 提升为 admin → bob 会话立即可调管理端点
    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._transport.app),
        base_url="http://testserver",
    )
    try:
        code_b = await make_invite(client)
        await register_tenant(bob, "bob", code_b)
        await bob.post(
            "/api/console/login", json={"username": "bob", "password": "password123"}
        )
        resp = await bob.get("/api/console/admin/users")
        assert resp.status_code == 403  # tenant 不能

        resp = await client.patch(
            "/api/console/admin/users/bob", json={"role": "admin"}, headers=ADMIN_HEADERS
        )
        assert resp.status_code == 200
        resp = await bob.get("/api/console/admin/users")
        assert resp.status_code == 200  # 提升即时生效

        # 降级 → 下一个请求立即 403（不信任会话载荷 r）
        resp = await client.patch(
            "/api/console/admin/users/bob",
            json={"role": "tenant"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        resp = await bob.get("/api/console/admin/users")
        assert resp.status_code == 403

        # 禁用 bob → bob 的租户面请求也立即 403；登录被拒
        resp = await client.patch(
            "/api/console/admin/users/bob",
            json={"disabled": True},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        resp = await bob.get("/api/console/me")
        assert resp.status_code == 403 and err_code(resp) == "account_disabled"
    finally:
        await bob.aclose()

    async with server.db.session() as session:
        user = await UserRepository(session).get_by_username("bob")
        assert user.role == "tenant" and user.disabled is True


@pytest.mark.asyncio
async def test_self_lockout_protection(env):
    """契约 v1.1：admin 会话不可禁用/降级自己（409 self_lockout）；key 通道不受限"""
    _, client = env
    code = await make_invite(client, role="admin")
    await register_tenant(client, "root", code)
    resp = await client.post(
        "/api/console/login", json={"username": "root", "password": "password123"}
    )
    assert resp.status_code == 200

    # 禁用自己 → 409
    resp = await client.patch(
        "/api/console/admin/users/root", json={"disabled": True}
    )
    assert resp.status_code == 409 and err_code(resp) == "self_lockout"

    # 降级自己 → 409
    resp = await client.patch("/api/console/admin/users/root", json={"role": "tenant"})
    assert resp.status_code == 409 and err_code(resp) == "self_lockout"

    # 允许：解除自己的禁用（若有）、提升自己为 admin（幂等）
    resp = await client.patch(
        "/api/console/admin/users/root", json={"disabled": False}
    )
    assert resp.status_code == 200
    resp = await client.patch("/api/console/admin/users/root", json={"role": "admin"})
    assert resp.status_code == 200

    # 允许：admin 会话禁用/降级**其他** admin
    other = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._transport.app),
        base_url="http://testserver",
    )
    try:
        code2 = await make_invite(client, role="admin")
        await register_tenant(other, "root2", code2)
        resp = await client.patch(
            "/api/console/admin/users/root2", json={"disabled": True}
        )
        assert resp.status_code == 200
    finally:
        await other.aclose()

    # admin key 通道不受 self_lockout 限制（key 无身份，服务器脚本场景）
    resp = await client.patch(
        "/api/console/admin/users/root",
        json={"disabled": True},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    resp = await client.patch(
        "/api/console/admin/users/root",
        json={"role": "tenant"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_invite_creation_persists(env):
    server, client = env
    resp = await client.post(
        "/api/console/admin/invites",
        json={"max_uses": 3, "expires_days": 7},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    code = resp.json()["code"]
    assert "-" in code  # 可读格式

    async with server.db.session() as session:
        invite = await InviteRepository(session).get_by_code(code)
        assert invite is not None
        assert invite.max_uses == 3 and invite.used_count == 0
        assert invite.role == "tenant"  # 契约 v1.1：默认 tenant
        assert invite.expires_at is not None
        remaining = invite.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)
        assert timedelta(days=6.9) < remaining < timedelta(days=7.01)

    # 邀请码可用于注册（max_uses=3 可用多次）
    for i in range(3):
        assert (await register_tenant(client, f"user-{i}", code)).status_code == 201
    resp = await register_tenant(client, "user-3", code)
    assert resp.status_code == 400 and err_code(resp) == "invite_exhausted"

    # 非法 role → 400
    resp = await client.post(
        "/api/console/admin/invites",
        json={"role": "superadmin"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400 and err_code(resp) == "invalid_role"


@pytest.mark.asyncio
async def test_role_invite_chain_admin_invite_grants_admin(env):
    """契约 v1.1 链路：admin key 签发 role=admin 邀请码 → 注册 → role=admin → 可管理"""
    server, client = env
    admin_code = await make_invite(client, role="admin")

    resp = await register_tenant(client, "invited-admin", admin_code)
    assert resp.status_code == 201
    assert resp.json()["role"] == "admin"  # 注册响应即按邀请码角色

    resp = await client.post(
        "/api/console/login",
        json={"username": "invited-admin", "password": "password123"},
    )
    assert resp.status_code == 200 and resp.json()["role"] == "admin"
    resp = await client.get("/api/console/me")
    assert resp.status_code == 200 and resp.json()["role"] == "admin"

    # admin 会话可调管理端点
    resp = await client.get("/api/console/admin/users")
    assert resp.status_code == 200

    # DB 落库 role=admin
    async with server.db.session() as session:
        user = await UserRepository(session).get_by_username("invited-admin")
        assert user.role == "admin"

    # tenant 邀请码注册仍为 tenant（默认角色回归）
    tenant_code = await make_invite(client)
    other = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._transport.app),
        base_url="http://testserver",
    )
    try:
        resp = await register_tenant(other, "plain-tenant", tenant_code)
        assert resp.json()["role"] == "tenant"
        # admin 邀请码用尽后不可复用为 admin
    finally:
        await other.aclose()


# ============== entry 模板（契约 §5） ==============


@pytest.mark.asyncio
async def test_entry_returns_template_for_frontend(env):
    _, client = env
    await register_and_login_client(client)

    resp = await client.get("/api/console/entry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entry_base"] == "https://tunely.example.com"
    qr = body["qr"]
    assert qr["v"] == 1 and qr["kind"] == "dsh-tunnel"
    assert qr["url"] == "https://{domain}"  # 占位符模板，前端替换后编码二维码
    assert "{token}" in qr["desktop"] and "tunely connect" in qr["desktop"]
    assert body["desktop_connect"] == qr["desktop"]


# ============== 兜底：ConsoleError handler 形状 ==============


@pytest.mark.asyncio
async def test_console_error_shape_via_handler():
    err = ConsoleError(418, "teapot", "short and stout")
    resp = await console_error_handler(None, err)
    assert resp.status_code == 418
    assert json.loads(resp.body) == {
        "error": {"code": "teapot", "message": "short and stout"}
    }
