#!/usr/bin/env python3
"""e2e 字节保真 / 数据面测量工具（docs/MIGRATION_TCP_ONLY.md §10-1）

三种模式：

  local            本地起 target + server + client 全套，经 TCP 监听跑字节保真矩阵
                   ——0.11 收敛的核心验收（JSON/二进制/gzip/SSE/大上传/嵌套 WS 逐字节一致）。
  local-httpproxy  同一套 harness，但探针改走 /t/{domain}/（HTTP 模式）——用已知真值
                   量化该数据面的损伤形态（1.0 删除前的对照锚）。
  measure --url    对现网 /t/ 入口做无损探测（无需控制 target），按损伤特征签名报告
                   ——切流前量化踩坑面用。

用法：
  python3 tools/e2e_dataplane.py local
  python3 tools/e2e_dataplane.py local-httpproxy
  python3 tools/e2e_dataplane.py measure --url https://dsht.example.com/t/dsh

退出码：0=全部通过/CLEAN，1=harness 错误，2=检出损伤。
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path

# 让脚本在未 pip install 的情况下直接消费仓内源码（python/tools/ → python/）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("tunely").setLevel(logging.ERROR)

# ============== 已知真值（ground truth） ==============

JSON_BODY = b'{"msg":"hello","n":123}'
GZIP_PLAIN = b'{"msg":"gzip-payload","n":456}'
GZIP_BODY = gzip.compress(GZIP_PLAIN)
BINARY_BODY = bytes(range(256))
SSE_BODY = (
    b"event: start\ndata: 1\n\n"
    b"event: delta\ndata: line1\nline2\n\n"  # 多行 data：破坏 SSE 帧的探针
    b"event: done\ndata: bye\n\n"
)
UPLOAD_BODY = (b"0123456789abcdef" * 65536)  # 1 MiB 确定性字节


def build_target_app() -> Starlette:
    """已知真值的目标服务（隧道另一端的「内网服务」）"""

    async def api_json(request):
        return Response(JSON_BODY, media_type="application/json")

    async def api_gzip(request):
        return Response(GZIP_BODY, media_type="application/json",
                        headers={"Content-Encoding": "gzip"})

    async def api_binary(request):
        return Response(BINARY_BODY, media_type="application/octet-stream")

    async def api_sse(request):
        async def gen():
            yield SSE_BODY
        return StreamingResponse(gen(), media_type="text/event-stream")

    async def api_echo(request):
        body = await request.body()
        return Response(b"echo:" + body, media_type="application/octet-stream")

    async def wsecho(websocket):
        await websocket.accept()
        try:
            while True:
                msg = await websocket.receive_text()
                if msg == "quit":
                    break
                await websocket.send_text(f"pong:{msg}")
        except Exception:
            pass

    return Starlette(routes=[
        Route("/api/json", api_json),
        Route("/api/gzip", api_gzip),
        Route("/api/binary", api_binary),
        Route("/api/sse", api_sse),
        Route("/api/echo", api_echo, methods=["POST"]),
        WebSocketRoute("/wsecho", wsecho),
    ])


# ============== 本地 harness（target + tunely server + client 一条龙） ==============

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _self_signed_cert(tmpdir: str) -> tuple[str, str]:
    """现造自签证书（仅测试用）：返回 (cert_path, key_path)"""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(tmpdir, "cert.pem")
    key_path = os.path.join(tmpdir, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


def _insecure_ssl_context() -> "ssl.SSLContext":  # noqa: F821
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _wait_http(url: str, timeout: float = 15.0) -> None:
    async with httpx.AsyncClient(trust_env=False) as c:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await c.get(url)
                return
            except Exception:
                await asyncio.sleep(0.15)
    raise RuntimeError(f"harness 启动超时: {url}")


class Harness:
    """一条龙：目标服务 + tunely 完整 app（含 TCP 监听与 /t/ 路由）+ 客户端"""

    def __init__(self, tunnel_mode: str = "tcp", tls: bool = False) -> None:
        self.tunnel_mode = tunnel_mode
        self.tls = tls
        self.target_port = _free_port()
        self.proxy_port = _free_port()
        self.tcp_port = _free_port()
        self._tasks: list[asyncio.Task] = []
        self.tmpdir = tempfile.mkdtemp(prefix="tunely-e2e-")
        self.cert_path: str | None = None
        self.key_path: str | None = None

    async def start(self) -> None:
        # TCP 监听经 env 注入（create_full_app 内 TunnelServerConfig 未显式传的
        # 字段走 WS_TUNNEL_ env，恰好绕开「create_full_app 只透传 5 字段」的限制）
        os.environ["WS_TUNNEL_TCP_LISTEN"] = f"{self.tcp_port}:demo"
        os.environ["WS_TUNNEL_TCP_LISTEN_HOST"] = "127.0.0.1"
        if self.tls:
            self.cert_path, self.key_path = _self_signed_cert(self.tmpdir)
            os.environ["WS_TUNNEL_LISTENER_TLS_CERT_FILE"] = self.cert_path
            os.environ["WS_TUNNEL_LISTENER_TLS_KEY_FILE"] = self.key_path

        from tunely.database import DatabaseManager
        from tunely.repository import TunnelRepository

        db_url = f"sqlite+aiosqlite:///{self.tmpdir}/t.db"
        dbm = DatabaseManager(db_url)
        await dbm.initialize()
        async with dbm.session() as session:
            tunnel = await TunnelRepository(session).create(
                domain="demo", mode=self.tunnel_mode)
            await session.commit()
        self.token = tunnel.token

        target = uvicorn.Server(uvicorn.Config(
            build_target_app(), host="127.0.0.1", port=self.target_port, log_level="error"))
        self._tasks.append(asyncio.create_task(target.serve()))

        from tunely.app import create_full_app
        app = create_full_app(domain="localhost", database_url=db_url, ws_path="/ws/tunnel")
        proxy = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.proxy_port, log_level="error"))
        self._tasks.append(asyncio.create_task(proxy.serve()))

        await _wait_http(f"http://127.0.0.1:{self.target_port}/api/json")
        await _wait_http(f"http://127.0.0.1:{self.proxy_port}/health")

        from tunely.client import TunnelClient, TunnelClientConfig
        client = TunnelClient(config=TunnelClientConfig(
            server_url=f"ws://127.0.0.1:{self.proxy_port}/ws/tunnel",
            token=self.token,
            target_url=f"http://127.0.0.1:{self.target_port}",
            reconnect_interval=1.0,
        ))
        self._tasks.append(asyncio.create_task(client.run()))

        # 等隧道注册完成
        async with httpx.AsyncClient(trust_env=False) as c:
            deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < deadline:
                r = await c.get(f"http://127.0.0.1:{self.proxy_port}/health")
                if r.json().get("connected_tunnels", 0) >= 1:
                    return
                await asyncio.sleep(0.2)
        raise RuntimeError("隧道客户端未在时限内注册")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    def tcp_base(self) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://127.0.0.1:{self.tcp_port}"

    def proxy_base(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    def proxy_t_base(self) -> str:
        return f"{self.proxy_base()}/t/demo"


# ============== 探针与判定 ==============

async def run_byte_fidelity(
    base_url: str, with_ws: bool = True, insecure_tls: bool = False
) -> tuple[list[str], list[str]]:
    """对 base_url 跑字节保真矩阵，返回 (passed, failed)。

    每个探针独立捕获异常：传输层损伤（连接被掐/协议错误）记为该探针的
    FAIL 而不是中断整个矩阵——local-httpproxy 模式下这正是预期损伤。
    insecure_tls=True 时按 https + 自签证书访问（local-tls 模式）。
    """
    passed: list[str] = []
    failed: list[str] = []
    client_kwargs: dict = dict(trust_env=False, timeout=30.0)
    if insecure_tls:
        client_kwargs["verify"] = False

    async def probe(name: str, coro_fn) -> None:
        try:
            ok, detail = await coro_fn()
        except Exception as e:  # 传输层损伤本身就是测量结果
            ok, detail = False, f"FAIL {type(e).__name__}: {str(e)[:110]}"
        (passed if ok else failed).append(f"{name}: {detail}")

    async with httpx.AsyncClient(**client_kwargs) as c:
        # 1. JSON 逐字节（HTTP 模式 0 字节事故的反向锚定）
        async def p_json():
            r = await c.get(f"{base_url}/api/json")
            return r.content == JSON_BODY, (
                "PASS" if r.content == JSON_BODY else
                f"FAIL got {r.content[:60]!r} ({len(r.content)}B, code={r.status_code})")
        await probe("json 逐字节", p_json)

        # 2. gzip：body 解压后一致 + content-encoding 头原样到达
        async def p_gzip():
            r = await c.get(f"{base_url}/api/gzip")
            body_ok = r.content == GZIP_PLAIN
            ce_header = r.headers.get("content-encoding", "")
            ok = body_ok and ce_header.lower() == "gzip"
            return ok, ("PASS" if ok else
                        f"FAIL body_ok={body_ok} content_encoding={ce_header!r}")
        await probe("gzip 保真", p_gzip)

        # 3. 二进制 256B 逐字节
        async def p_binary():
            r = await c.get(f"{base_url}/api/binary")
            ok = r.content == BINARY_BODY
            return ok, ("PASS" if ok else
                        f"FAIL got {len(r.content)}B, head={r.content[:24]!r}")
        await probe("binary 256B", p_binary)

        # 4. SSE 原始字节（事件边界 + event: 类型保真；按文档语义带 Accept 走流路径）
        async def p_sse():
            async with c.stream("GET", f"{base_url}/api/sse",
                                headers={"Accept": "text/event-stream"}) as r:
                raw = b"".join([chunk async for chunk in r.aiter_raw()])
            ok = raw == SSE_BODY
            return ok, ("PASS" if ok else f"FAIL got {raw[:60]!r}")
        await probe("SSE 原始字节", p_sse)

        # 5. 1MiB 上传流式回显（逐字节对比）
        async def p_upload():
            r = await c.post(f"{base_url}/api/echo", content=UPLOAD_BODY)
            ok = r.content == b"echo:" + UPLOAD_BODY
            return ok, ("PASS" if ok else
                        f"FAIL got {len(r.content)}B, sha={hashlib.sha256(r.content).hexdigest()[:12]}")
        await probe("upload 1MiB 回显", p_upload)

    # 6. 嵌套 WebSocket 经字节管道
    if with_ws:
        async def p_ws():
            try:
                from websockets.asyncio.client import connect
            except ImportError:  # 旧版 websockets
                from websockets import connect  # type: ignore
            scheme = "wss" if insecure_tls else "ws"
            rest = base_url.split("://", 1)[1]
            ws_url = f"{scheme}://{rest}/wsecho"
            extra = {"ssl": _insecure_ssl_context()} if insecure_tls else {}
            async with connect(ws_url, open_timeout=10, **extra) as ws:
                await ws.send("hello")
                reply = await asyncio.wait_for(ws.recv(), timeout=10)
            return reply == "pong:hello", ("PASS" if reply == "pong:hello" else f"FAIL reply={reply!r}")
        await probe("嵌套 WS", p_ws)

    return passed, failed


async def assert_alpn_locks_http11(host: str, port: int) -> tuple[bool, str]:
    """ALPN 锁验证：客户端同时提供 h2 与 http/1.1，服务端必须选中 http/1.1

    这是对「TLS 终止后字节原样入隧道」的关键保护——协商出 h2 而内网目标
    只讲 HTTP/1.1 即连接废掉（docs/MIGRATION_TCP_ONLY.md §5）。
    """
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    reader, writer = await asyncio.open_connection(
        host, port, ssl=ctx, server_hostname="localhost")
    try:
        selected = writer.get_extra_info("ssl_object").selected_alpn_protocol()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    ok = selected == "http/1.1"
    return ok, (f"PASS（selected={selected!r}，h2 被正确拒绝）" if ok
                else f"FAIL selected={selected!r}（应为 http/1.1）")


async def check_control_plane_tls(cert_path: str, key_path: str, port: int) -> tuple[bool, str]:
    """控制面 TLS 验证：uvicorn 挂证书起 HTTPS，GET /ping 应 200（run_app 同款接线）"""
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
        ssl_certfile=cert_path, ssl_keyfile=key_path))
    task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient(trust_env=False, verify=False, timeout=10.0) as c:
            deadline = asyncio.get_event_loop().time() + 10.0
            while True:
                try:
                    r = await c.get(f"https://127.0.0.1:{port}/ping")
                    break
                except Exception:
                    if asyncio.get_event_loop().time() > deadline:
                        raise
                    await asyncio.sleep(0.15)
        return r.status_code == 200, f"{'PASS' if r.status_code == 200 else f'FAIL status={r.status_code}'}（https://127.0.0.1:{port}/ping）"
    except Exception as e:
        return False, f"FAIL {type(e).__name__}: {str(e)[:100]}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except Exception:
            task.cancel()


def judge_remote_response(r: httpx.Response) -> list[str]:
    """对无真值的远端响应做损伤特征签名判定（measure 模式）"""
    flags: list[str] = []
    content = r.content
    declared = r.headers.get("content-length")

    if r.status_code == 200 and len(content) == 0 and declared not in (None, "0"):
        flags.append(f"200-但-0-字节（连接被掐断; declared CL={declared}）")
    if declared is not None and declared.isdigit() and int(declared) != len(content):
        flags.append(f"content-length 失配（declared={declared}, got={len(content)}）")
    ctype = r.headers.get("content-type", "")
    if "json" in ctype and content:
        try:
            json.loads(content)
        except (json.JSONDecodeError, ValueError):
            flags.append("application/json 但 body 非法 JSON（截断/重序列化签名）")
    if "gzip" in r.headers.get("content-encoding", "").lower() and content:
        if not content.startswith(b"\x1f\x8b"):
            flags.append("content-encoding: gzip 但 body 非 gzip 字节（解压后仍透传该头）")
    return flags


# ============== 三种模式 ==============

async def mode_local() -> int:
    h = Harness()
    await h.start()
    try:
        print(f"== local: TCP 数据面字节保真（{h.tcp_base()}，经 WS_TUNNEL_TCP_LISTEN）==")
        passed, failed = await run_byte_fidelity(h.tcp_base())
        _report(passed, failed)
        return 0 if not failed else 2
    finally:
        await h.stop()


async def mode_local_httpproxy() -> int:
    # 对照模式测的是「遗留 HTTP 模式」的损伤形态，隧道须建成 mode='http'
    # （0.11 起 mode 恒写 tcp 的新隧道，/t/ 命中会被 410 快速失败，见 app.py 守卫）
    h = Harness(tunnel_mode="http")
    await h.start()
    try:
        print(f"== local-httpproxy: 同一真值经 /t/ HTTP 模式（{h.proxy_t_base()}）——损伤对照 ==")
        passed, failed = await run_byte_fidelity(h.proxy_t_base())
        _report(passed, failed)
        print("\n说明：以上 FAIL 即 HTTP 数据面（JSON 承载 body）的实测损伤形态，"
              "迁移完成后该入口将整体删除（docs/MIGRATION_TCP_ONLY.md）。")
        return 0 if not failed else 2
    finally:
        await h.stop()


async def mode_local_tls() -> int:
    h = Harness(tls=True)
    await h.start()
    try:
        print(f"== local-tls: TCP 数据面原生 TLS（{h.tcp_base()}，自签证书 + ALPN 锁）==")
        passed, failed = await run_byte_fidelity(h.tcp_base(), insecure_tls=True)

        # ALPN 锁：客户端同时提供 h2 与 http/1.1，必须选中 http/1.1
        try:
            ok, detail = await assert_alpn_locks_http11("127.0.0.1", h.tcp_port)
        except Exception as e:
            ok, detail = False, f"FAIL {type(e).__name__}: {str(e)[:100]}"
        (passed if ok else failed).append(f"ALPN 锁 http/1.1: {detail}")

        # 控制面 TLS（run_app 同款 uvicorn 接线）
        ok, detail = await check_control_plane_tls(
            h.cert_path, h.key_path, _free_port())
        (passed if ok else failed).append(f"控制面 TLS(HTTPS): {detail}")

        _report(passed, failed)
        return 0 if not failed else 2
    finally:
        await h.stop()


async def mode_measure(url: str, paths: list[str]) -> int:
    print(f"== measure: 现网 HTTP 模式探测（{url}）——损伤特征签名 ==")
    damaged = 0
    checked = 0
    async with httpx.AsyncClient(trust_env=False, timeout=20.0, follow_redirects=False) as c:
        for p in paths:
            full = url.rstrip("/") + p
            try:
                r = await c.get(full)
            except Exception as e:
                print(f"  {p:<28} TRANSPORT-ERROR {type(e).__name__}: {str(e)[:90]}")
                damaged += 1
                checked += 1
                continue
            flags = judge_remote_response(r)
            checked += 1
            status = "DAMAGED" if flags else "clean  "
            print(f"  {p:<28} {status} status={r.status_code} bytes={len(r.content)}")
            for f in flags:
                print(f"      - {f}")
            damaged += 1 if flags else 0
    print(f"\n结论: {checked} 个探针, {damaged} 个检出损伤签名"
          + ("" if damaged else "（未见损伤特征——不代表语义无损，仅未见可自动判定的签名）"))
    return 2 if damaged else 0


def _report(passed: list[str], failed: list[str]) -> None:
    for line in passed + failed:
        print(f"  {line}")
    print(f"\n结论: {len(passed)} PASS / {len(failed)} FAIL")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("local", help="TCP 数据面字节保真验收（全绿为收敛验收通过）")
    sub.add_parser("local-tls", help="原生 TLS：自签证书 + TLS 监听复跑矩阵 + ALPN 锁 + 控制面 HTTPS")
    sub.add_parser("local-httpproxy", help="同一真值经 /t/ HTTP 模式（损伤对照）")
    m = sub.add_parser("measure", help="对现网 /t/ 入口做损伤特征探测")
    m.add_argument("--url", required=True, help="如 https://dsht.example.com/t/dsh")
    m.add_argument("--path", action="append", default=["/", "/api/health"],
                   help="探测路径（可多次）")
    args = parser.parse_args()

    try:
        if args.mode == "local":
            return asyncio.run(mode_local())
        if args.mode == "local-tls":
            return asyncio.run(mode_local_tls())
        if args.mode == "local-httpproxy":
            return asyncio.run(mode_local_httpproxy())
        return asyncio.run(mode_measure(args.url, args.path))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
