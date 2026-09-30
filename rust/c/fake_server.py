#!/usr/bin/env python3
"""最小假隧道服务端——只为验证 C ABI 端到端（**不是** tunely 服务端实现）。

流程：接连接 → 读 auth → 回 auth_ok → 下发一条 request → 校验客户端回包 → 退出。

- 校验通过：退出码 0
- 校验失败 / 超时：退出码 1，并把收到的帧打到 stdout
- 用法：python3 rust/c/fake_server.py [port]

（输出全部 flush：作为后台进程时 stdout 是块缓冲，不 flush 会看不到结果。）
"""

import asyncio
import json
import sys

import websockets


def log(message: str) -> None:
    print(f"[fake-server] {message}", flush=True)


async def handle(ws, done: asyncio.Future) -> None:
    try:
        auth = json.loads(await ws.recv())
        log(f"auth: {auth.get('type')} client_version={auth.get('client_version')}")
        await ws.send(
            json.dumps({"type": "auth_ok", "domain": "c-demo.test", "tunnel_id": "t-c"})
        )
        await ws.send(
            json.dumps(
                {
                    "type": "request",
                    "id": "req-c-1",
                    "method": "POST",
                    "path": "/embed/echo",
                    "headers": {"x-demo": "1"},
                    "body": '{"hello":"world"}',
                }
            )
        )

        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        log(f"got: {json.dumps(msg, ensure_ascii=False, sort_keys=True)}")

        # 客户端已经走了，用「是否收到合法回包」判定成败
        assert msg["type"] == "response", msg
        assert msg["id"] == "req-c-1", msg
        assert msg["status"] == 200, msg
        assert msg["headers"].get("x-tunely-embed") == "c", msg
        assert msg["headers"].get("content-type") == "application/json", msg
        assert json.loads(msg["body"]) == {
            "from": "c-host",
            "method": "POST",
            "path": "/embed/echo",
        }, msg
        log("OK")
        if not done.done():
            done.set_result(True)
    except Exception as e:  # noqa: BLE001 —— 测试工具，任何异常都要变成失败信号
        log(f"FAILED: {type(e).__name__}: {e}")
        if not done.done():
            done.set_exception(e)


async def main(port: int) -> None:
    done: asyncio.Future = asyncio.get_running_loop().create_future()
    async with websockets.serve(
        lambda ws: handle(ws, done), "127.0.0.1", port, close_timeout=1
    ):
        log(f"listening on ws://127.0.0.1:{port}/ws/tunnel")
        await asyncio.wait_for(done, timeout=25)


if __name__ == "__main__":
    try:
        asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8765))
    except (asyncio.TimeoutError, AssertionError, Exception) as exc:  # noqa: BLE001
        log(f"EXIT-FAIL: {type(exc).__name__}: {exc}")
        sys.exit(1)
