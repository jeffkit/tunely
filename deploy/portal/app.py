"""
Tunely 门户鉴权服务（dsht 站点登录页 + 会话校验）

- GET  /login        登录页（表单）
- POST /login        校验用户名/密码（portal-users.json，sha256），签发 HMAC 会话 Cookie
- GET  /auth         供 nginx auth_request 子请求调用：Cookie 有效 204 / 无效 401
- GET  /logout       清除 Cookie 并跳转登录页

安全：Cookie = base64url(payload).base64url(HMAC-SHA256)，HttpOnly+SameSite=Lax+Secure；
     next 参数仅接受站内路径（防开放重定向）；登录失败延迟 1s。
配置：/etc/tunely/portal-secret（HMAC 密钥）、/etc/tunely/portal-users.json（{"用户":"sha256(密码)"}）
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import html
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

SECRET = Path("/etc/tunely/portal-secret").read_bytes().strip()
USERS = json.loads(Path("/etc/tunely/portal-users.json").read_text())
COOKIE = "dsht_session"
TTL = 7 * 86400

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dsht 隧道门户 — 登录</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; }
  body { min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #0f1115; color: #e8eaf0;
         font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  .card { width: 340px; padding: 36px 32px; background: #171a21; border: 1px solid #262b36;
          border-radius: 12px; }
  h1 { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
  .sub { font-size: 12px; color: #8a93a6; margin-bottom: 24px; }
  label { display: block; font-size: 12px; color: #8a93a6; margin: 14px 0 6px; }
  input { width: 100%; padding: 10px 12px; font-size: 14px; color: #e8eaf0;
          background: #0f1115; border: 1px solid #2c3342; border-radius: 8px; outline: none; }
  input:focus { border-color: #3b82f6; }
  button { width: 100%; margin-top: 22px; padding: 11px; font-size: 14px; font-weight: 600;
           color: #fff; background: #2563eb; border: 0; border-radius: 8px; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  .err { margin-top: 14px; font-size: 12px; color: #f87171; }
  .foot { margin-top: 22px; font-size: 11px; color: #566072; text-align: center; }
</style></head><body>
<div class="card">
  <h1>dsht 隧道门户</h1>
  <div class="sub">登录后可访问本域名下的受保护服务</div>
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{next_escaped}">
    <label for="u">用户名</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">密码</label>
    <input id="p" name="password" type="password" autocomplete="current-password">
    <button type="submit">登 录</button>
  </form>
  {error_html}
  <div class="foot">tunely portal</div>
</div></body></html>"""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _issue_cookie(response: Response, user: str) -> None:
    payload = json.dumps({"u": user, "exp": int(time.time()) + TTL}).encode()
    mac = hmac.new(SECRET, payload, hashlib.sha256).digest()
    response.set_cookie(
        COOKIE,
        f"{_b64e(payload)}.{_b64e(mac)}",
        max_age=TTL,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )


def _cookie_user(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        p, m = token.split(".", 1)
        payload = _b64d(p)
        if not hmac.compare_digest(_b64d(m), hmac.new(SECRET, payload, hashlib.sha256).digest()):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        user = data.get("u")
        return user if user in USERS else None
    except Exception:
        return None


def _safe_next(next_url: str | None) -> str:
    """仅接受站内绝对路径，防开放重定向"""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _login_page(next_url: str, error: str | None = None) -> HTMLResponse:
    err_html = f'<div class="err">{error}</div>' if error else ""
    return HTMLResponse(
        PAGE.replace("{next_escaped}", html.escape(next_url, quote=True))
        .replace("{error_html}", err_html)
    )


@app.get("/auth")
async def auth(request: Request) -> Response:
    if _cookie_user(request):
        return Response(status_code=204)
    return Response(status_code=401)


@app.get("/login")
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    if _cookie_user(request):
        return RedirectResponse(_safe_next(next), status_code=302)
    return _login_page(next)


@app.post("/login")
async def login(request: Request) -> Response:
    await asyncio.sleep(1)  # 失败延迟：防爆破（成功路径无感）
    form = urllib.parse.parse_qs((await request.body()).decode())
    user = form.get("username", [""])[0]
    password = form.get("password", [""])[0]
    next_url = _safe_next(form.get("next", ["/"])[0])

    expect = USERS.get(user, "")
    ok = expect and hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), expect)
    if not ok:
        return _login_page(next_url, error="用户名或密码错误")

    response = Response(status_code=303, headers={"Location": next_url})
    _issue_cookie(response, user)
    return response


@app.get("/logout")
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE, path="/")
    return response

