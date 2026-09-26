"""
TCP 连接表清理回归测试（F6）

目标侧先关闭连接时，TcpConnection 的 _read_loop 结束后必须：
1. 回执恰好一条 tcp_close；
2. 关闭本地 writer 释放 fd；
3. 从 TunnelClient._tcp_connections 移除条目。

原实现 finally 只 _send_close() 不移除条目，目标侧先关连接时
连接对象与 fd 永久滞留，形成泄漏。
"""

import asyncio
import base64

from tunely.client import TcpConnection, TunnelClient
from tunely.config import TunnelClientConfig
from tunely.protocol import TcpCloseMessage, TcpConnectMessage, TcpDataMessage


class FakeWebsocket:
    """记录发送的 wire 消息（JSON 字符串），用于断言回执"""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)


class EchoTarget:
    """
    回环 TCP 目标服务（127.0.0.1 随机端口）。

    close_immediately=True: 接受连接后立即关闭（模拟目标侧先关，触发 F6 场景）。
    否则 echo 模式：收到的数据原样返回。
    跟踪全部已接受的 writer，stop() 时逐一关闭（Python 3.13 的
    server.wait_closed() 会等待 handler 结束，必须先断开活跃连接才不悬挂）。
    """

    def __init__(self, close_immediately: bool = False):
        self.close_immediately = close_immediately
        self.server: asyncio.AbstractServer | None = None
        self.port: int = 0
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> "EchoTarget":
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            self._writers.add(writer)
            try:
                if self.close_immediately:
                    writer.close()
                    return
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            finally:
                self._writers.discard(writer)
                try:
                    writer.close()
                except Exception:
                    pass

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close_client_connections(self) -> None:
        """目标侧主动断开全部已接受连接（触发客户端读 EOF）"""
        for w in list(self._writers):
            w.close()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
        await self.close_client_connections()
        if self.server is not None:
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass


async def wait_until(pred, timeout: float = 2.0) -> None:
    """轮询等待条件成立，超时抛 AssertionError"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def make_client(target_port: int) -> TunnelClient:
    """构造指向本地目标的 TunnelClient（不真正连服务端，只测 TCP 模式处理路径）"""
    config = TunnelClientConfig(
        server_url="ws://localhost:8000/ws/tunnel",
        token="test-token",
        target_url=f"http://127.0.0.1:{target_port}",
    )
    return TunnelClient(config=config)


def sent_of(ws: FakeWebsocket, msg_type: str) -> list[str]:
    return [m for m in ws.sent if f'"type":"{msg_type}"' in m]


class TestTcpConnectionTableCleanup:
    """目标侧先关连接 → 连接表条目被移除（F6）"""

    async def test_target_close_removes_entry_and_closes_writer(self):
        target = await EchoTarget(close_immediately=True).start()
        try:
            ws = FakeWebsocket()
            client = make_client(target.port)

            # 走真实装配路径：_handle_tcp_connect 建连并挂上清理回调
            await client._handle_tcp_connect(TcpConnectMessage(conn_id="c1"), ws)
            assert "c1" in client._tcp_connections
            conn = client._tcp_connections["c1"]

            # 目标立即关闭 → _read_loop EOF → 完整收尾
            await wait_until(lambda: "c1" not in client._tcp_connections)

            # 恰好回执一条 tcp_close
            await asyncio.sleep(0.05)  # 再等一拍，确认无重复回执
            closes = sent_of(ws, "tcp_close")
            assert len(closes) == 1

            # writer 已关闭（fd 释放）
            assert conn._writer is not None
            assert conn._writer.is_closing()
        finally:
            await target.stop()

    async def test_cleanup_callback_invoked_once_on_eof(self):
        """TcpConnection 级别：EOF 后清理回调恰好调用一次"""
        target = await EchoTarget(close_immediately=True).start()
        try:
            ws = FakeWebsocket()
            removed: list[str] = []
            conn = TcpConnection(
                conn_id="c2",
                target_host="127.0.0.1",
                target_port=target.port,
                websocket=ws,
                on_closed=lambda c: removed.append(c.conn_id),
            )
            assert await conn.connect() is True

            await wait_until(lambda: len(removed) == 1)
            await asyncio.sleep(0.05)
            assert removed == ["c2"]  # 恰好一次
            assert conn._closed is True
            # 读取结束也应回执 tcp_close
            assert len(sent_of(ws, "tcp_close")) == 1
        finally:
            await target.stop()

    async def test_echo_still_works_before_close(self):
        """回归：清理逻辑不破坏正常数据通路（echo 后条目仍在，目标关闭后才移除）"""
        target = await EchoTarget().start()
        try:
            ws = FakeWebsocket()
            client = make_client(target.port)

            await client._handle_tcp_connect(TcpConnectMessage(conn_id="c3"), ws)
            assert "c3" in client._tcp_connections

            await client._handle_tcp_data(
                TcpDataMessage(
                    conn_id="c3",
                    data=base64.b64encode(b"ping").decode(),
                    sequence=0,
                )
            )
            await wait_until(lambda: len(sent_of(ws, "tcp_data")) >= 1)

            # 数据通路正常，条目仍在表中
            assert "c3" in client._tcp_connections

            # 目标侧先关连接 → 客户端读 EOF → 条目被移除
            await target.close_client_connections()
            await wait_until(lambda: "c3" not in client._tcp_connections)
        finally:
            await target.stop()

    async def test_identity_guard_keeps_reused_conn_id_entry(self):
        """按对象身份移除：同 conn_id 的新连接不会被旧连接的收尾误删"""
        ws = FakeWebsocket()
        client = make_client(9)  # 端口无所谓，不真正建连

        old = TcpConnection(conn_id="c4", target_host="h", target_port=1, websocket=ws)
        new = TcpConnection(conn_id="c4", target_host="h", target_port=1, websocket=ws)
        client._tcp_connections["c4"] = new

        # 旧连接对象触发清理回调 → 不应误删表中同 id 的新条目
        old._on_closed = client._remove_tcp_connection
        old._notify_closed()

        assert client._tcp_connections.get("c4") is new

    async def test_server_initiated_close_still_removes_entry(self):
        """回归：服务端发起 tcp_close 的原有路径不受影响"""
        target = await EchoTarget().start()
        try:
            ws = FakeWebsocket()
            client = make_client(target.port)

            await client._handle_tcp_connect(TcpConnectMessage(conn_id="c5"), ws)
            assert "c5" in client._tcp_connections

            await client._handle_tcp_close(TcpCloseMessage(conn_id="c5"))
            assert "c5" not in client._tcp_connections
        finally:
            await target.stop()
