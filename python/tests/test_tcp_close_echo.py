"""
TCP 关闭回声回归测试（2026-09-29「未知连接风暴」根因修复）

客户端（目标侧）先发起的 tcp_close 会让服务端 _handle_tcp_connection
的读任务被取消、进入 finally。原实现无条件回发 tcp_close——而客户端在
发送关闭前就已清理本地映射，回声只能被当「未知连接」丢弃；churn 期
（keepalive 空闲连接批量回收）即成回声风暴，双向日志互刷。

修复后语义：
1. 仅当移除动作发生在服务端（remove_tcp_connection 返回 True）才回发
   tcp_close——客户端先关则不回发（测试一）；
2. 服务端（外部侧）先关仍恰好回发一条 tcp_close，行为不变（测试二）。
"""

import socket
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tunely.app import create_lifespan
from tunely.config import TunnelServerConfig
from tunely.server import TunnelServer


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _make_server(domain: str, port: int) -> TunnelServer:
    return TunnelServer(
        config=TunnelServerConfig(
            domain="echo.test",
            database_url="sqlite+aiosqlite:///:memory:",
            admin_api_key="k",
            tcp_listen=f"{port}:{domain}",
        )
    )


def _create_tunnel(client: TestClient, domain: str) -> str:
    resp = client.post(
        "/api/tunnels", json={"domain": domain}, headers={"x-api-key": "k"}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["token"]


class TestTcpCloseEcho:
    def test_client_initiated_close_does_not_echo_tcp_close(self):
        """客户端先发 tcp_close：之后 WS 上不得再出现 tcp_close（用 ping/pong 探序）"""
        port = _free_port()
        domain = "echo-close-a"
        server = _make_server(domain, port)
        app = FastAPI(lifespan=create_lifespan(server))
        app.include_router(server.router)

        with TestClient(app) as client:
            token = _create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json({"type": "auth", "token": token})
                ok = ws.receive_json()
                assert ok["type"] == "auth_ok", ok

                with socket.create_connection(("127.0.0.1", port), timeout=5) as ext:
                    connect_msg = ws.receive_json()
                    assert connect_msg["type"] == "tcp_connect"
                    conn_id = connect_msg["conn_id"]

                    # 客户端（目标侧）先发起关闭
                    ws.send_json({"type": "tcp_close", "conn_id": conn_id})
                    ext.close()
                    time.sleep(0.3)

                    # 探序：修复前 finally 会回发 tcp_close 并排在 pong 之前
                    ws.send_json({"type": "ping"})
                    nxt = ws.receive_json()
                    assert nxt["type"] == "pong", f"预期 pong，实际 {nxt}"

    def test_server_initiated_close_sends_exactly_one_tcp_close(self):
        """外部侧先关（客户端未关）：仍恰好回发一条 tcp_close（行为不变）"""
        port = _free_port()
        domain = "echo-close-b"
        server = _make_server(domain, port)
        app = FastAPI(lifespan=create_lifespan(server))
        app.include_router(server.router)

        with TestClient(app) as client:
            token = _create_tunnel(client, domain)
            with client.websocket_connect("/ws/tunnel") as ws:
                ws.send_json({"type": "auth", "token": token})
                ok = ws.receive_json()
                assert ok["type"] == "auth_ok", ok

                with socket.create_connection(("127.0.0.1", port), timeout=5) as ext:
                    connect_msg = ws.receive_json()
                    assert connect_msg["type"] == "tcp_connect"
                    conn_id = connect_msg["conn_id"]

                    ext.close()
                    close_msg = ws.receive_json()
                    assert close_msg["type"] == "tcp_close", close_msg
                    assert close_msg["conn_id"] == conn_id

                # 恰好一条：后续只应有心跳应答
                ws.send_json({"type": "ping"})
                nxt = ws.receive_json()
                assert nxt["type"] == "pong", f"预期 pong，实际 {nxt}"
