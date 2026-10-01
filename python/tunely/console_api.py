"""
多租户自助控制台 API（Console v2）

权威契约：docs/CONSOLE_MULTITENANT.md（v1）。本模块实现契约 §5 的全部端点：

租户面（会话 cookie 鉴权）：
- POST /api/console/register           邀请码注册
- POST /api/console/login              登录（失败固定延迟防爆破）
- POST /api/console/logout             登出（清 cookie）
- GET  /api/console/me                 当前用户
- GET  /api/console/tunnels            我的隧道列表（不含 token）
- POST /api/console/tunnels            创建隧道（配额/域名冲突 409；token 明文仅此处返回）
- POST /api/console/tunnels/{domain}/rotate-token   轮换 token（旧 token 失效 + 踢连接）
- DELETE /api/console/tunnels/{domain} 删除隧道（断开在线连接）
- GET  /api/console/entry              接入说明模板（供前端渲染二维码）

管理面（复用既有 admin key 鉴权，x-api-key）：
- GET  /api/console/admin/users        列出用户
- POST /api/console/admin/users        禁用/启用用户
- POST /api/console/admin/invites      签发邀请码

安全决策（契约 §3/§4/§5）：
- 密码哈希 stdlib hashlib.scrypt（n=2^14, r=8, p=1，16B 随机盐），
  存储格式 `scrypt$n$r$p$salt$hash`（hex）。
- 会话为无状态 HMAC-SHA256 签名 cookie（不建 session 表）：
  `console_session`，载荷 {u, r, exp}，Max-Age 7 天，
  HttpOnly; SameSite=Lax; Path=/（Secure 交给部署层 TLS 终止处）。
- 所有权：tenant 只能触碰 owner_id == 自己 的隧道，越权一律 404（不泄露存在性）。
- token 明文只在 create / rotate-token 响应出现。
- 登录失败固定延迟（console_login_failure_delay，默认 1s），用户名不存在与
  密码错误/账号禁用走同一错误路径，不泄露账号存在性。

错误统一 `{"error": {"code", "message"}}`（ConsoleError + app 级 handler，
只对控制台抛出的 ConsoleError 生效，不影响既有 admin 通道的错误形状）。

entry 模板约定：qr.url/desktop 中的 `{domain}`/`{token}`/`{target}` 为占位符，
由前端用实际隧道域名（prefix + "." + entry_base 的 host）、create/rotate 响应中的
token 明文、以及租户目标服务地址替换后再编码二维码（契约 §6）。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import TunnelServerConfig
from .models import Invite, Tunnel, User
from .repository import TunnelRepository

if TYPE_CHECKING:
    from .server import TunnelServer

logger = logging.getLogger(__name__)

# ============== 常量与校验规则（契约 §3） ==============

USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]{3,32}$")
# 隧道前缀：与 TunnelServer.DOMAIN_PATTERN 同宽度（1-63），收敛为小写（Host 头大小写敏感）
PREFIX_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128

# scrypt 参数（契约 §3：n=2^14, r=8, p=1，16B 盐）
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_BYTES = 16
_SCRYPT_DKLEN = 64

# 邀请码字符表（去掉 0/o/1/l/i 等易混淆字符，可读格式如 dsh-x7k2-9f3a）
_INVITE_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def _utcnow_naive() -> datetime:
    """naive UTC 时间戳（DB 列为无时区 DATETIME，统一存 UTC）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============== 密码哈希（契约 §3） ==============


def hash_password(password: str) -> str:
    """scrypt 哈希，格式 `scrypt$n$r$p$salt$hash`（均 hex）"""
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码（格式不符/解析失败一律 False，常数时间比较）"""
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ============== 无状态 HMAC 会话 cookie（契约 §4） ==============


class ConsoleSessionManager:
    """`console_session` cookie 的签发/校验

    载荷 `{u: username, r: role, exp: unix_ts}`，JSON → base64url，
    HMAC-SHA256 签名（`payload.sig`）。无服务端状态，改密码/禁用后旧会话
    仍持有效签名，由 me/tunnels 每请求查库 disabled 拦截（契约 §5 me disabled → 403）。
    """

    COOKIE_NAME = "console_session"
    MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 天（契约 §4）

    def __init__(self, secret: bytes):
        self._secret = secret

    def issue(self, username: str, role: str, now: float | None = None) -> str:
        current = time.time() if now is None else now
        payload = {
            "u": username,
            "r": role,
            "exp": int(current + self.MAX_AGE_SECONDS),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_b64 = self._b64encode(raw)
        sig = hmac.new(self._secret, payload_b64, hashlib.sha256).hexdigest()
        return f"{payload_b64.decode('ascii')}.{sig}"

    def verify(self, token: str | None) -> dict[str, Any] | None:
        """校验签名与有效期；无效返回 None"""
        if not token or "." not in token:
            return None
        payload_b64, _, sig = token.rpartition(".")
        expected = hmac.new(
            self._secret, payload_b64.encode("ascii", errors="ignore"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            payload = json.loads(self._b64decode(payload_b64))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or exp < time.time():
            return None
        if not isinstance(payload.get("u"), str) or not isinstance(payload.get("r"), str):
            return None
        return payload

    def set_cookie(self, response: Response, token: str) -> None:
        # HttpOnly/SameSite=Lax/Path=/（契约 §4）；Secure 交给部署层 TLS 终止处加
        response.set_cookie(
            self.COOKIE_NAME,
            token,
            max_age=self.MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(self.COOKIE_NAME, path="/")

    @staticmethod
    def _b64encode(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    @staticmethod
    def _b64decode(data: str) -> bytes:
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding)


def resolve_session_secret(config: TunnelServerConfig) -> bytes:
    """解析会话签名密钥（契约 §4：server 既有 secret 文件机制，无则配置项）

    优先级：console_session_secret > console_session_secret_file（无文件则
    生成并写回，重启后会话连续）> 进程内随机（重启后全部会话失效，打警告）。
    """
    if config.console_session_secret:
        return config.console_session_secret.encode("utf-8")

    if config.console_session_secret_file:
        path = Path(config.console_session_secret_file)
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text.encode("utf-8")
        generated = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated + "\n", encoding="utf-8")
        logger.info(f"已生成控制台会话密钥文件: {path}")
        return generated.encode("utf-8")

    logger.warning(
        "console_session_secret 未配置：控制台会话使用进程内随机密钥，重启后所有会话失效；"
        "生产部署请设置 WS_TUNNEL_CONSOLE_SESSION_SECRET（或 *_FILE）"
    )
    return secrets.token_bytes(32)


# ============== 统一错误（契约 §5：{"error": {"code","message"}}） ==============


class ConsoleError(Exception):
    """控制台 API 业务错误；经 console_error_handler 渲染为统一错误形状"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


async def console_error_handler(request: Request, exc: ConsoleError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


_TModel = TypeVar("_TModel", bound=BaseModel)


async def _parse_body(request: Request, model: type[_TModel]) -> _TModel:
    """解析 JSON 请求体；非法 JSON/字段错误统一 400（不走 FastAPI 422 形状）"""
    try:
        raw = await request.json()
    except Exception:
        raise ConsoleError(400, "bad_request", "请求体必须是合法 JSON")
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        detail = e.errors()[0].get("msg", "invalid")
        raise ConsoleError(400, "bad_request", f"请求体字段不合法: {detail}")


# ============== 请求模型 ==============


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str
    invite_code: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


class CreateTunnelByTenantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str


class AdminUserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    disabled: bool


class AdminInviteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_days: float | None = Field(default=None, gt=0, le=3650)


# ============== 用户/邀请码数据访问 ==============


class UserRepository:
    """users 表数据访问"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_username(self, username: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.username == username)
        )
        return result.scalar_one_or_none()

    async def create(self, username: str, password_hash: str, role: str = "tenant") -> User:
        user = User(username=username, password_hash=password_hash, role=role)
        self.session.add(user)
        await self.session.flush()
        return user

    async def list_all(self) -> list[User]:
        result = await self.session.execute(select(User).order_by(User.id))
        return list(result.scalars().all())

    async def set_disabled(self, username: str, disabled: bool) -> bool:
        from sqlalchemy import update as sql_update

        result = await self.session.execute(
            sql_update(User).where(User.username == username).values(disabled=disabled)
        )
        return result.rowcount > 0


class InviteRepository:
    """invites 表数据访问"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_code(self, code: str) -> Invite | None:
        result = await self.session.execute(select(Invite).where(Invite.code == code))
        return result.scalar_one_or_none()

    async def create(
        self,
        code: str,
        created_by: str = "admin",
        max_uses: int = 1,
        expires_at: datetime | None = None,
    ) -> Invite:
        invite = Invite(
            code=code,
            created_by=created_by,
            max_uses=max_uses,
            used_count=0,
            expires_at=expires_at,
        )
        self.session.add(invite)
        await self.session.flush()
        return invite

    async def consume(self, code: str) -> bool:
        """原子消耗一次使用额度（带 used_count < max_uses 守卫，防并发超发）"""
        result = await self.session.execute(
            update(Invite)
            .where(Invite.code == code, Invite.used_count < Invite.max_uses)
            .values(used_count=Invite.used_count + 1)
        )
        return result.rowcount > 0


def generate_invite_code() -> str:
    """可读邀请码：xxxx-xxxx（去易混淆字符表）"""
    rng = secrets.SystemRandom()

    def part() -> str:
        return "".join(rng.choice(_INVITE_ALPHABET) for _ in range(4))

    return f"{part()}-{part()}"


def invite_is_expired(invite: Invite, now: datetime | None = None) -> bool:
    current = now or _utcnow_naive()
    return invite.expires_at is not None and invite.expires_at < current


# ============== 路由构建 ==============


def mount_console_api(app: Any, server: "TunnelServer") -> None:
    """把控制台路由挂载进 FastAPI app 并注册统一错误 handler（app.py 调用）"""
    app.include_router(build_console_router(server))
    app.add_exception_handler(ConsoleError, console_error_handler)


def build_console_router(server: "TunnelServer") -> APIRouter:
    """构建 /api/console 路由（绑定 TunnelServer 实例：db/manager/config/审计）"""
    config = server.config
    sessions = ConsoleSessionManager(resolve_session_secret(config))
    router = APIRouter(prefix="/api/console", tags=["Console"])

    def _require_db():
        if server.db is None:
            raise ConsoleError(500, "server_error", "数据库未初始化")
        return server.db

    def _session_payload(request: Request) -> dict[str, Any]:
        token = request.cookies.get(ConsoleSessionManager.COOKIE_NAME)
        payload = sessions.verify(token)
        if payload is None:
            raise ConsoleError(401, "unauthorized", "会话无效或已过期")
        return payload

    async def _require_active_user(request: Request) -> User:
        """会话 → 用户；签名有效但用户已删除 → 401，已禁用 → 403"""
        payload = _session_payload(request)
        db = _require_db()
        async with db.session() as session:
            user = await UserRepository(session).get_by_username(payload["u"])
        if user is None:
            raise ConsoleError(401, "unauthorized", "用户不存在或已删除")
        if user.disabled:
            raise ConsoleError(403, "account_disabled", "账号已被禁用")
        return user

    async def _get_owned_tunnel(user: User, domain: str) -> Tunnel:
        """取 user 所有的隧道；不存在或非本人 → 一律 404（不泄露存在性，契约 §5）"""
        db = _require_db()
        async with db.session() as session:
            tunnel = await TunnelRepository(session).get_by_domain(domain)
        if tunnel is None or tunnel.owner_id != user.id:
            raise ConsoleError(404, "not_found", "隧道不存在")
        return tunnel

    # ---------- 注册 ----------

    @router.post("/register", status_code=201)
    async def register(request: Request) -> Response:
        body = await _parse_body(request, RegisterRequest)
        if not USERNAME_PATTERN.match(body.username or ""):
            raise ConsoleError(
                400, "invalid_username", "用户名须为 3-32 位小写字母/数字/下划线/连字符"
            )
        if not (PASSWORD_MIN_LENGTH <= len(body.password or "") <= PASSWORD_MAX_LENGTH):
            raise ConsoleError(
                400,
                "invalid_password",
                f"密码长度须为 {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} 位",
            )
        invite_code = (body.invite_code or "").strip()
        if not invite_code:
            raise ConsoleError(400, "invite_invalid", "邀请码无效")

        db = _require_db()
        async with db.session() as session:
            users = UserRepository(session)
            invites = InviteRepository(session)

            if await users.get_by_username(body.username) is not None:
                raise ConsoleError(409, "username_taken", "用户名已被占用")

            invite = await invites.get_by_code(invite_code)
            if invite is None:
                raise ConsoleError(400, "invite_invalid", "邀请码无效")
            if invite_is_expired(invite):
                raise ConsoleError(400, "invite_expired", "邀请码已过期")
            # 原子消耗（used_count+1 带守卫）；用尽/并发抢完 → invite_exhausted
            if not await invites.consume(invite_code):
                raise ConsoleError(400, "invite_exhausted", "邀请码已被用尽")

            user = await users.create(body.username, hash_password(body.password))
            # session 上下文退出时统一 commit（失败整体回滚，含消耗额度）

        return JSONResponse(
            status_code=201, content={"username": user.username, "role": user.role}
        )

    # ---------- 登录 / 登出 / me ----------

    @router.post("/login")
    async def login(request: Request) -> Response:
        body = await _parse_body(request, LoginRequest)
        db = _require_db()
        authenticated: User | None = None
        async with db.session() as session:
            user = await UserRepository(session).get_by_username(
                (body.username or "").strip()
            )
            if (
                user is not None
                and not user.disabled
                and verify_password(body.password or "", user.password_hash)
            ):
                authenticated = user

        if authenticated is None:
            # 固定延迟防爆破（契约 §4）；不存在/密码错/被禁用同路径，不泄露账号状态
            delay = config.console_login_failure_delay
            if delay and delay > 0:
                await asyncio.sleep(delay)
            raise ConsoleError(401, "invalid_credentials", "用户名或密码错误")

        response = JSONResponse(
            content={"username": authenticated.username, "role": authenticated.role}
        )
        sessions.set_cookie(response, sessions.issue(authenticated.username, authenticated.role))
        return response

    @router.post("/logout", status_code=204)
    async def logout(request: Request) -> Response:
        _session_payload(request)  # 会话无效 → 401
        response = Response(status_code=204)
        sessions.clear_cookie(response)
        return response

    @router.get("/me")
    async def me(request: Request) -> dict:
        payload = _session_payload(request)
        db = _require_db()
        async with db.session() as session:
            user = await UserRepository(session).get_by_username(payload["u"])
        if user is None:
            raise ConsoleError(401, "unauthorized", "用户不存在或已删除")
        if user.disabled:
            raise ConsoleError(403, "account_disabled", "账号已被禁用")
        return {"username": user.username, "role": user.role, "disabled": False}

    # ---------- 隧道自助管理 ----------

    @router.get("/tunnels")
    async def list_my_tunnels(request: Request) -> list[dict]:
        user = await _require_active_user(request)
        db = _require_db()
        async with db.session() as session:
            tunnels = await TunnelRepository(session).list_by_owner(user.id)
        # 契约 §5：不含 token；online/流量与 admin 通道同源（manager 内存统计）
        return [
            {
                "domain": t.domain,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "online": server.manager.is_connected(t.domain),
                "last_seen_at": (
                    t.last_connected_at.isoformat() if t.last_connected_at else None
                ),
                "bytes_in": server._tunnel_byte_stats(t.domain)["bytes_in"],
                "bytes_out": server._tunnel_byte_stats(t.domain)["bytes_out"],
            }
            for t in tunnels
        ]

    @router.post("/tunnels", status_code=201)
    async def create_my_tunnel(request: Request) -> Response:
        user = await _require_active_user(request)
        body = await _parse_body(request, CreateTunnelByTenantRequest)
        prefix = (body.prefix or "").strip().lower()
        if not PREFIX_PATTERN.match(prefix):
            raise ConsoleError(
                400,
                "invalid_prefix",
                "前缀须为 1-63 位小写字母/数字/连字符，且以字母或数字开头结尾",
            )

        quota = config.console_tunnels_per_user
        db = _require_db()
        async with db.session() as session:
            repo = TunnelRepository(session)
            if quota <= 0 or await repo.count_by_owner(user.id) >= quota:
                raise ConsoleError(409, "quota_exceeded", f"隧道数量已达配额（{quota}）")
            if await repo.get_by_domain(prefix) is not None:
                raise ConsoleError(409, "domain_taken", "该域名前缀已被占用")
            # 0.11 起新隧道恒为 tcp（与 admin 通道一致，docs/MIGRATION_TCP_ONLY.md §4.4）
            tunnel = await repo.create(domain=prefix, owner_id=user.id, mode="tcp")
            token = tunnel.token
            await session.commit()

        # 审计在提交后写（SQLite 同库第二会话会被未提交写锁阻塞，见 server._delete_tunnel 注释）
        from .server import _client_ip

        await server._record_audit(
            "create",
            domain=tunnel.domain,
            detail="mode=tcp;via=console",
            source_ip=_client_ip(request),
        )
        # token 明文仅 create / rotate-token 响应返回（契约 §5）
        return JSONResponse(
            status_code=201, content={"domain": tunnel.domain, "token": token}
        )

    @router.post("/tunnels/{domain}/rotate-token", status_code=201)
    async def rotate_my_token(domain: str, request: Request) -> Response:
        user = await _require_active_user(request)
        tunnel = await _get_owned_tunnel(user, domain)

        db = _require_db()
        async with db.session() as session:
            new_token = await TunnelRepository(session).regenerate_token(tunnel.domain)
            if new_token is None:
                raise ConsoleError(404, "not_found", "隧道不存在")
            await session.commit()

        # 旧 token 立即失效：断开存量连接（复用 server 现有 ActiveConnection 管理路径）
        await server._close_tunnel_connection(tunnel.domain, reason="token rotated")

        from .server import _client_ip

        await server._record_audit(
            "regenerate",
            domain=tunnel.domain,
            detail="rotated;via=console",
            source_ip=_client_ip(request),
        )
        return JSONResponse(
            status_code=201, content={"domain": tunnel.domain, "token": new_token}
        )

    @router.delete("/tunnels/{domain}", status_code=204)
    async def delete_my_tunnel(domain: str, request: Request) -> Response:
        user = await _require_active_user(request)
        tunnel = await _get_owned_tunnel(user, domain)

        db = _require_db()
        async with db.session() as session:
            deleted = await TunnelRepository(session).delete(tunnel.domain)
            if not deleted:
                raise ConsoleError(404, "not_found", "隧道不存在")

        # 在线连接断开（契约 §5）
        await server._close_tunnel_connection(tunnel.domain, reason="tunnel revoked")

        from .server import _client_ip

        await server._record_audit(
            "delete",
            domain=tunnel.domain,
            detail="revoked;via=console",
            source_ip=_client_ip(request),
        )
        return Response(status_code=204)

    # ---------- 接入说明模板 ----------

    @router.get("/entry")
    async def entry(request: Request) -> dict:
        await _require_active_user(request)
        ws_url = config.ws_url or f"wss://{config.domain}{config.ws_path}"
        connect_cmd = f"tunely connect --server {ws_url} --token {{token}} --target {{target}}"
        return {
            "entry_base": f"https://{config.domain}",
            "qr": {
                "v": 1,
                "kind": "dsh-tunnel",
                "url": "https://{domain}",
                "desktop": connect_cmd,
                "note": "占位符 {domain}/{token}/{target} 由前端替换实际值后编码二维码",
            },
            "desktop_connect": connect_cmd,
        }

    # ---------- 管理面（复用 admin key 鉴权，契约 §5） ----------

    @router.get("/admin/users")
    async def admin_list_users(
        x_api_key: str | None = Header(None, alias="x-api-key"),
    ) -> list[dict]:
        server._check_admin_api_key(x_api_key)
        db = _require_db()
        async with db.session() as session:
            users = await UserRepository(session).list_all()
        return [
            {
                "username": u.username,
                "role": u.role,
                "disabled": u.disabled,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]

    @router.post("/admin/users")
    async def admin_update_user(
        request: Request,
        x_api_key: str | None = Header(None, alias="x-api-key"),
    ) -> dict:
        server._check_admin_api_key(x_api_key)
        body = await _parse_body(request, AdminUserUpdateRequest)
        db = _require_db()
        async with db.session() as session:
            ok = await UserRepository(session).set_disabled(body.username, body.disabled)
        if not ok:
            raise ConsoleError(404, "not_found", "用户不存在")
        return {"username": body.username, "disabled": body.disabled}

    @router.post("/admin/invites")
    async def admin_create_invite(
        request: Request,
        x_api_key: str | None = Header(None, alias="x-api-key"),
    ) -> dict:
        server._check_admin_api_key(x_api_key)
        body = await _parse_body(request, AdminInviteCreateRequest)
        expires_at = (
            _utcnow_naive() + timedelta(days=body.expires_days)
            if body.expires_days is not None
            else None
        )
        db = _require_db()
        # 码空间 ~3.6e10（30^8 分组），碰撞概率极低；仍做有限重试兜底唯一约束
        for _ in range(5):
            try:
                async with db.session() as session:
                    invite = await InviteRepository(session).create(
                        code=generate_invite_code(),
                        max_uses=body.max_uses,
                        expires_at=expires_at,
                    )
                break
            except IntegrityError:
                continue
        else:
            raise ConsoleError(500, "server_error", "邀请码生成失败（碰撞重试耗尽）")

        return {"code": invite.code}

    return router
