"""
JWT 认证单元测试

测试 TunnelServer 的 JWT Bearer 认证功能，包括：
- 无 jwt_secret 时跳过认证（向后兼容）
- 有效 JWT 认证通过
- 缺少 Authorization 头时拒绝
- 无效格式的 Authorization 头拒绝
- 过期 Token 拒绝
- 签名不匹配的 Token 拒绝
- /api/info 接口的 auth 字段
"""

import time

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from tunely.config import TunnelServerConfig
from tunely.server import TunnelServer


JWT_SECRET = "test-secret-key-for-unit-tests"


class TestJWTVerification:
    """测试 _verify_jwt_token 方法"""

    def _make_server(self, jwt_secret: str | None = None) -> TunnelServer:
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            jwt_secret=jwt_secret,
        )
        return TunnelServer(config=config)

    def _make_token(
        self,
        secret: str = JWT_SECRET,
        payload: dict | None = None,
        expired: bool = False,
    ) -> str:
        data = payload or {"sub": "test-user", "name": "Test"}
        if expired:
            data["exp"] = int(time.time()) - 3600
        elif "exp" not in data:
            data["exp"] = int(time.time()) + 3600
        return pyjwt.encode(data, secret, algorithm="HS256")

    def test_no_jwt_secret_skips_auth(self):
        """无 jwt_secret 时直接返回 None，不校验（向后兼容）"""
        server = self._make_server(jwt_secret=None)
        result = server._verify_jwt_token(None)
        assert result is None

    def test_no_jwt_secret_skips_even_with_header(self):
        """无 jwt_secret 时即使带了 Authorization 也跳过"""
        server = self._make_server(jwt_secret=None)
        result = server._verify_jwt_token("Bearer some-token")
        assert result is None

    def test_valid_token_returns_payload(self):
        """有效 JWT 返回 payload 字典"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        token = self._make_token()
        result = server._verify_jwt_token(f"Bearer {token}")
        assert result is not None
        assert result["sub"] == "test-user"
        assert result["name"] == "Test"

    def test_missing_authorization_raises_401(self):
        """有 jwt_secret 但缺少 Authorization 头时返回 401"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        with pytest.raises(HTTPException) as exc_info:
            server._verify_jwt_token(None)
        assert exc_info.value.status_code == 401
        assert "Authorization header required" in exc_info.value.detail

    def test_empty_authorization_raises_401(self):
        """空 Authorization 头返回 401"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        with pytest.raises(HTTPException) as exc_info:
            server._verify_jwt_token("")
        assert exc_info.value.status_code == 401

    def test_invalid_format_no_bearer_raises_401(self):
        """非 Bearer 格式的 Authorization 头返回 401"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        with pytest.raises(HTTPException) as exc_info:
            server._verify_jwt_token("Basic dXNlcjpwYXNz")
        assert exc_info.value.status_code == 401
        assert "Invalid authorization format" in exc_info.value.detail

    def test_invalid_format_bearer_only_raises_401(self):
        """只有 'Bearer' 没有 token 部分返回 401"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        with pytest.raises(HTTPException) as exc_info:
            server._verify_jwt_token("Bearer")
        assert exc_info.value.status_code == 401

    def test_expired_token_raises_401(self):
        """过期 Token 返回 401"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        token = self._make_token(expired=True)
        with pytest.raises(HTTPException) as exc_info:
            server._verify_jwt_token(f"Bearer {token}")
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    def test_wrong_secret_raises_401(self):
        """用错误密钥签名的 Token 返回 401"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        token = self._make_token(secret="wrong-secret")
        with pytest.raises(HTTPException) as exc_info:
            server._verify_jwt_token(f"Bearer {token}")
        assert exc_info.value.status_code == 401
        assert "Invalid token" in exc_info.value.detail

    def test_malformed_token_raises_401(self):
        """格式错误的 Token 返回 401"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        with pytest.raises(HTTPException) as exc_info:
            server._verify_jwt_token("Bearer not-a-valid-jwt-token")
        assert exc_info.value.status_code == 401

    def test_bearer_case_insensitive(self):
        """Bearer 前缀不区分大小写"""
        server = self._make_server(jwt_secret=JWT_SECRET)
        token = self._make_token()
        result = server._verify_jwt_token(f"bearer {token}")
        assert result is not None
        assert result["sub"] == "test-user"


class TestJWTConfigBackwardCompatibility:
    """测试 JWT 配置的向后兼容性"""

    def test_config_default_jwt_secret_is_none(self):
        """默认 jwt_secret 为 None"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
        )
        assert config.jwt_secret is None

    def test_config_explicit_jwt_secret(self):
        """显式设置 jwt_secret"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            jwt_secret="my-secret",
        )
        assert config.jwt_secret == "my-secret"

    def test_server_without_jwt_creates_normally(self):
        """没有 jwt_secret 的服务器正常创建"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
        )
        server = TunnelServer(config=config)
        assert server.config.jwt_secret is None
        assert server.router is not None


class TestCreateTunnelWithJWT:
    """测试创建隧道时的 JWT 认证集成"""

    @pytest.mark.asyncio
    async def test_create_tunnel_without_jwt_secret(self):
        """无 jwt_secret 配置时，创建隧道不需要认证"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
        )
        server = TunnelServer(config=config)
        await server.initialize()

        try:
            from tunely.server import CreateTunnelRequest
            request = CreateTunnelRequest(
                domain="test-no-auth",
                name="Test No Auth",
            )
            result = await server._create_tunnel(request, api_key=None, authorization=None)
            assert result.domain == "test-no-auth"
            assert result.token is not None
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_create_tunnel_with_valid_jwt(self):
        """有 jwt_secret 且提供有效 JWT 时，创建隧道成功"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            jwt_secret=JWT_SECRET,
        )
        server = TunnelServer(config=config)
        await server.initialize()

        try:
            from tunely.server import CreateTunnelRequest
            token = pyjwt.encode(
                {"sub": "test-user", "exp": int(time.time()) + 3600},
                JWT_SECRET,
                algorithm="HS256",
            )
            request = CreateTunnelRequest(
                domain="test-with-auth",
                name="Test With Auth",
            )
            result = await server._create_tunnel(
                request, api_key=None, authorization=f"Bearer {token}"
            )
            assert result.domain == "test-with-auth"
            assert result.token is not None
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_create_tunnel_with_jwt_secret_but_no_token(self):
        """有 jwt_secret 但不提供 token 时，创建隧道失败"""
        config = TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            jwt_secret=JWT_SECRET,
        )
        server = TunnelServer(config=config)
        await server.initialize()

        try:
            from tunely.server import CreateTunnelRequest
            request = CreateTunnelRequest(
                domain="test-no-token",
                name="Test No Token",
            )
            with pytest.raises(HTTPException) as exc_info:
                await server._create_tunnel(request, api_key=None, authorization=None)
            assert exc_info.value.status_code == 401
        finally:
            await server.close()
