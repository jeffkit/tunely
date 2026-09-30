"""
WS-Tunnel 客户端 SDK

提供隧道客户端功能，可独立运行或嵌入到应用中

使用示例:
    from tunely import TunnelClient

    client = TunnelClient(
        server_url="ws://server/ws/tunnel",
        token="tun_xxx",
        target_url="http://localhost:8080"
    )

    # 启动客户端（阻塞）
    await client.run()

    # 或在后台运行
    task = asyncio.create_task(client.run())
"""

import asyncio
import base64
import json
import logging
import time
from datetime import datetime
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from .config import TunnelClientConfig
from .protocol import (
    AuthErrorMessage,
    AuthMessage,
    AuthOkMessage,
    MessageType,
    PingMessage,
    PongMessage,
    TunnelRequest,
    TunnelResponse,
    StreamStartMessage,
    StreamChunkMessage,
    StreamEndMessage,
    TcpConnectMessage,
    TcpDataMessage,
    TcpCloseMessage,
    UdpCloseMessage,
    UdpOpenMessage,
    decode_tcp_data_frame,
    decode_udp_data_frame,
    encode_tcp_data_frame,
    encode_udp_data_frame,
    parse_message,
    parse_message_fast,
    stream_chunk_payload,
    stream_end_payload,
    stream_start_payload,
    tcp_close_payload,
    tcp_data_payload,
    udp_close_payload,
    dump_payload,
    FRAME_TYPE_UDP_DATA,
)

logger = logging.getLogger(__name__)


def normalize_path(path: str) -> str:
    """归一化服务端下发的请求路径：确保以 "/" 开头。

    防止 "@evil/" 这类不以 "/" 开头的 path 在 URL 拼接时改写 authority
    （如 ``http://127.0.0.1:3080`` + ``@evil/`` → 请求打到 evil 主机，SSRF）。
    """
    if path.startswith("/"):
        return path
    return "/" + path


class TcpConnection:
    """
    单个 TCP 连接管理
    
    管理一个 TCP 连接的生命周期，包括：
    - 连接到目标服务
    - 读取数据并发送到服务端
    - 接收数据并写入到目标服务
    - 连接关闭处理
    """

    def __init__(self, conn_id: str, target_host: str, target_port: int, websocket,
                 on_closed: Optional[Callable[["TcpConnection"], None]] = None,
                 binary_frames: bool = False):
        """
        初始化 TCP 连接
        
        Args:
            conn_id: 连接唯一 ID
            target_host: 目标主机
            target_port: 目标端口
            websocket: WebSocket 连接（用于发送数据回服务端）
            on_closed: 读取结束（EOF/错误）后的清理回调（F6：从客户端连接表移除，防 fd 泄漏）
            binary_frames: 本连接是否协商了 binary_frames 能力（协议 v2；
                启用时 tcp_data 走 WS 二进制帧，否则 JSON+base64）
        """
        self.conn_id = conn_id
        self.target_host = target_host
        self.target_port = target_port
        self._websocket = websocket
        self._on_closed = on_closed
        self._binary_frames = binary_frames
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._sequence = 0
        self._closed = False

    async def connect(self) -> bool:
        """
        连接到目标服务
        
        Returns:
            是否连接成功
        """
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
            logger.info(f"TCP 连接已建立: {self.conn_id} -> {self.target_host}:{self.target_port}")
            
            # 启动读取任务
            self._read_task = asyncio.create_task(self._read_loop())
            return True
        except Exception as e:
            logger.error(f"TCP 连接失败: {self.conn_id} -> {self.target_host}:{self.target_port}, {e}")
            await self._send_close(str(e))
            return False

    async def _read_loop(self) -> None:
        """持续读取 TCP 数据并发送到服务端"""
        try:
            while not self._closed and self._reader:
                # 读取数据
                data = await self._reader.read(4096)
                if not data:
                    # 连接关闭
                    break
                
                # 发送到服务端
                await self._send_data(data)
                self._sequence += 1
        except Exception as e:
            logger.error(f"TCP 读取错误: {self.conn_id}, {e}")
        finally:
            # F6：本地侧读结束（EOF/错误）也要完整收尾——回执 tcp_close、
            # 关闭 writer、并从客户端连接表移除，否则目标侧先关连接时条目
            # 永久滞留在 _tcp_connections，fd 泄漏。
            await self._send_close()
            if not self._closed:
                self._closed = True
                if self._writer:
                    try:
                        self._writer.close()
                    except Exception as e:
                        logger.error(f"关闭 TCP writer 错误: {self.conn_id}, {e}")
                self._notify_closed()

    def _notify_closed(self) -> None:
        """触发读取结束回调（幂等：仅在 read_loop 收尾时调用一次）"""
        if self._on_closed:
            try:
                self._on_closed(self)
            except Exception as e:
                logger.error(f"TCP 连接清理回调失败: {self.conn_id}, {e}")

    async def _send_data(self, data: bytes) -> None:
        """发送数据到服务端

        协商 binary_frames 时发 WS 二进制帧（协议 v2，去 base64+JSON 开销）；
        否则走 0.7.3 的 JSON 路径（数据面快速路径：手工 dict，wire 键集不变）。
        """
        try:
            if self._binary_frames:
                await self._websocket.send(encode_tcp_data_frame(self.conn_id, data))
                return
            message = tcp_data_payload(
                self.conn_id,
                base64.b64encode(data).decode("ascii"),
                self._sequence,
            )
            await self._websocket.send(dump_payload(message))
        except Exception as e:
            logger.error(f"发送 TCP 数据失败: {self.conn_id}, {e}")

    async def _send_close(self, error: Optional[str] = None) -> None:
        """发送关闭消息到服务端"""
        if self._closed:
            return

        try:
            message = tcp_close_payload(self.conn_id, error)
            await self._websocket.send(dump_payload(message))
        except Exception as e:
            logger.error(f"发送 TCP 关闭消息失败: {self.conn_id}, {e}")

    async def write_data(self, data: bytes) -> None:
        """写入数据到目标服务"""
        if self._closed or not self._writer:
            return
        
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as e:
            logger.error(f"TCP 写入错误: {self.conn_id}, {e}")
            await self.close(str(e))

    async def close(self, error: Optional[str] = None) -> None:
        """关闭连接"""
        if self._closed:
            return
        
        logger.info(f"TCP 连接关闭: {self.conn_id}")

        # 先发送关闭消息（在设置 _closed 之前）
        if not self._closed:
            try:
                message = tcp_close_payload(self.conn_id, error)
                await self._websocket.send(dump_payload(message))
            except Exception as e:
                logger.error(f"发送 TCP 关闭消息失败: {self.conn_id}, {e}")
        
        # 设置关闭标志
        self._closed = True
        
        # 取消读取任务
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        
        # 关闭 writer
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.error(f"关闭 TCP writer 错误: {e}")


class _UdpTargetProtocol(asyncio.DatagramProtocol):
    """到目标服务的 UDP socket 协议（客户端侧，协议 v2 udp）

    datagram_received 是同步回调不能 await——入有界队列，由 UdpSession 的
    pump 任务顺序消费回发 0x03 帧（队满丢包，UDP 有损语义）。
    """

    def __init__(self, session: "UdpSession"):
        self.session = session
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.session.enqueue_inbound(data)

    def error_received(self, exc: Exception | None) -> None:
        # 连接型 UDP socket 的 ICMP 错误（如目标端口不可达）在此上报：
        # 交给会话异步收尾（发 udp_close + 关闭），不抛进事件循环
        self.session.report_error(exc)


class UdpSession:
    """
    单个 UDP 会话管理（协议 v2 udp）

    每会话一个到目标服务的 UDP socket：
    - 服务端 → 客户端方向（0x03 帧解出 payload）由消息循环调 send_to_target
      直接 sendto 目标；
    - 目标 → 服务端方向回包入有界队列，pump 任务顺序回发 0x03 帧
      （队满丢包——UDP 有损语义，不反压不积压）。
    """

    def __init__(
        self,
        session_id: str,
        websocket,
        queue_maxsize: int = 64,
        on_error=None,
    ):
        self.session_id = session_id
        self._websocket = websocket
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        self._on_error = on_error
        self._transport: asyncio.DatagramTransport | None = None
        self._pump_task: asyncio.Task | None = None
        self._closed = False

    async def connect(self, target_host: str, target_port: int) -> bool:
        """建立到目标的 UDP socket 并启动回包 pump 任务"""
        try:
            loop = asyncio.get_event_loop()
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _UdpTargetProtocol(self),
                remote_addr=(target_host, target_port),
            )
        except Exception as e:
            logger.error(
                f"UDP socket 建立失败: {self.session_id} -> {target_host}:{target_port}, {e}"
            )
            return False
        self._pump_task = asyncio.create_task(self._pump_loop())
        logger.info(
            f"UDP 会话已建立: {self.session_id} -> {target_host}:{target_port}"
        )
        return True

    def enqueue_inbound(self, data: bytes) -> None:
        """目标回包入队（队满丢弃本包——UDP 有损语义，不积压内存）"""
        if self._closed:
            return
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.debug(
                f"UDP 回包队列已满，丢包: session_id={self.session_id}, size={len(data)}"
            )

    def report_error(self, exc: Exception | None) -> None:
        """socket 错误上报（ICMP 不可达等）：异步收尾，不在回调里 await"""
        if self._closed:
            return
        logger.warning(f"UDP socket 错误: session_id={self.session_id}, {exc}")
        if self._on_error is not None:
            try:
                result = self._on_error(self, exc)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception as e:
                logger.error(f"UDP 会话错误收尾失败: session_id={self.session_id}, {e}")

    async def _pump_loop(self) -> None:
        """回包 pump：队列 → 0x03 二进制帧回发服务端"""
        while True:
            data = await self._queue.get()
            if data is None:
                break
            try:
                await self._websocket.send(encode_udp_data_frame(self.session_id, data))
            except Exception as e:
                logger.error(f"发送 UDP 数据失败: session_id={self.session_id}, {e}")
                break

    async def send_to_target(self, data: bytes) -> None:
        """服务端方向数据写入目标 socket"""
        if self._closed or self._transport is None:
            return
        try:
            self._transport.sendto(data)
        except Exception as e:
            logger.error(
                f"UDP 写入目标失败: session_id={self.session_id}, {e}"
            )
            self.report_error(e)

    async def close(self) -> None:
        """关闭会话（幂等）：关 socket、停 pump"""
        if self._closed:
            return
        self._closed = True
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception as e:
                logger.error(f"关闭 UDP socket 错误: session_id={self.session_id}, {e}")
            self._transport = None
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None


class TunnelClient:
    """
    隧道客户端

    连接到隧道服务器，接收请求并转发到本地目标服务
    """

    def __init__(
        self,
        server_url: str | None = None,
        token: str | None = None,
        target_url: str | None = None,
        config: TunnelClientConfig | None = None,
    ):
        """
        初始化客户端

        Args:
            server_url: 服务端 WebSocket URL
            token: 隧道令牌
            target_url: 本地目标服务 URL
            config: 客户端配置（可选，优先级低于直接参数）
        """
        if config:
            self.config = config
        else:
            self.config = TunnelClientConfig(
                server_url=server_url or "ws://localhost:8000/ws/tunnel",
                token=token or "",
                target_url=target_url or "http://localhost:8080",
            )

        self._websocket = None
        self._running = False
        self._connected = False
        self._domain: str | None = None
        self._reconnect_count = 0
        # 协议 v2 能力协商结果（AuthOk.capabilities 的本连接快照；
        # 重连重新认证后刷新，断线后按新连接语义重建）
        self._negotiated: frozenset[str] = frozenset()

        # 实例级共享 httpx 客户端（0.7.3：连接池跨请求/重连复用；
        # 0.7.2 及之前每请求新建 AsyncClient，TLS 握手无法复用）
        self._http: Optional[httpx.AsyncClient] = None

        # TCP 连接管理（TCP 模式使用）
        self._tcp_connections: Dict[str, TcpConnection] = {}

        # UDP 会话管理（协议 v2 udp）：session_id → UdpSession
        self._udp_sessions: Dict[str, UdpSession] = {}
        
        # 目标服务解析（TCP 模式使用）
        self._target_host: str = "localhost"
        self._target_port: int = 8080
        self._parse_target_url()

        # 回调函数
        self._on_connect: Callable[[], None] | None = None
        self._on_disconnect: Callable[[], None] | None = None
        self._on_request: Callable[[TunnelRequest], None] | None = None

    def _parse_target_url(self) -> None:
        """解析目标 URL，提取主机和端口（用于 TCP 模式）"""
        try:
            parsed = urlparse(self.config.target_url)
            self._target_host = parsed.hostname or "localhost"
            self._target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except Exception as e:
            logger.warning(f"解析目标 URL 失败: {e}，使用默认值")
            self._target_host = "localhost"
            self._target_port = 8080

    @property
    def _log_prefix(self) -> str:
        """多隧道模式日志前缀；单隧道为空串（日志与 0.9.x 逐字节一致）"""
        name = getattr(self.config, "name", None)
        return f"[{name}] " if name else ""

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected

    @property
    def domain(self) -> str | None:
        """分配的域名"""
        return self._domain

    def on_connect(self, callback: Callable[[], None]) -> None:
        """设置连接成功回调"""
        self._on_connect = callback

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        """设置断开连接回调"""
        self._on_disconnect = callback

    def on_request(self, callback: Callable[[TunnelRequest], None]) -> None:
        """设置请求接收回调"""
        self._on_request = callback

    async def run(self) -> None:
        """
        运行客户端

        自动重连，直到调用 stop()
        """
        self._running = True

        while self._running:
            try:
                await self._connect_and_run()
            except Exception as e:
                if not self._running:
                    break

                self._connected = False
                if self._on_disconnect:
                    self._on_disconnect()

                self._reconnect_count += 1
                max_attempts = self.config.max_reconnect_attempts

                if max_attempts > 0 and self._reconnect_count > max_attempts:
                    logger.error(f"{self._log_prefix}超过最大重连次数 ({max_attempts})，停止")
                    break

                logger.warning(
                    f"{self._log_prefix}连接断开: {e}，{self.config.reconnect_interval}秒后重连 "
                    f"(第 {self._reconnect_count} 次)"
                )
                await asyncio.sleep(self.config.reconnect_interval)

    async def stop(self) -> None:
        """停止客户端"""
        self._running = False
        if self._websocket:
            await self._websocket.close()
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def _get_http(self) -> httpx.AsyncClient:
        """共享 httpx 客户端（懒创建；超时在每请求上单独传）"""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient()
        return self._http

    @staticmethod
    def _client_version() -> str:
        """真实客户端版本（服务端记录用于升级核对；历史版本不传被默认成假值 0.1.0）

        优先取执行代码自身的 tunely.__version__（install 元数据可能过期谎报）。
        """
        try:
            import tunely

            return tunely.__version__
        except Exception:
            try:
                from importlib.metadata import version as _pkg_version

                return _pkg_version("tunely")
            except Exception:
                return "unknown"

    @staticmethod
    def _client_capabilities() -> list[str]:
        """本客户端已实现并声明的能力（协议 v2 能力协商）。

        铁律：只许声明已实现的能力（声明了没实现 = 服务端会用而客户端解析不了）。
        T2 起实现 binary_frames；T3 起实现 chunked_http（非 SSE 大响应流式回传）；
        T4 起实现 udp（UDP 会话透传）。新能力实现后在此追加。
        """
        return ["binary_frames", "chunked_http", "udp"]

    async def _connect_and_run(self) -> None:
        """连接并运行"""
        logger.info(f"正在连接到 {self.config.server_url}...")

        async with websockets.connect(
            self.config.server_url,
            ping_interval=30,
            ping_timeout=10,
        ) as websocket:
            self._websocket = websocket

            # 发送认证
            auth_message = AuthMessage(
                token=self.config.token,
                client_version=self._client_version(),
                force=self.config.force,
                capabilities=self._client_capabilities(),
            )
            await websocket.send(auth_message.model_dump_json())

            # 等待认证响应
            raw_response = await asyncio.wait_for(
                websocket.recv(),
                timeout=30.0,
            )
            data = json.loads(raw_response)
            response = parse_message(data)

            if isinstance(response, AuthErrorMessage):
                raise Exception(f"认证失败: {response.error}")

            if isinstance(response, AuthOkMessage):
                self._domain = response.domain
                # 协议 v2：协商结果 = AuthOk.capabilities（缺字段 = 空集合，
                # 旧服务端不带该字段时全 JSON，行为与 0.7.3 一致）
                self._negotiated = frozenset(
                    getattr(response, "capabilities", None) or ()
                )
                self._connected = True
                self._reconnect_count = 0

                logger.info(f"已连接: domain={self._domain}")

                if self._on_connect:
                    self._on_connect()

                # 消息循环（WS 断连时同步回收本连接的全部 UDP 会话——
                # 协议 v2 udp：socket 不随重连跨连接复用）
                try:
                    await self._message_loop(websocket)
                finally:
                    await self._close_all_udp_sessions()

    async def _message_loop(self, websocket) -> None:
        """消息处理循环"""
        async for raw_message in websocket:
            try:
                # 协议 v2 binary_frames：websockets 库迭代中 bytes = 二进制消息
                # （str = 文本消息）。协商了该能力才解帧路由；未协商收到 bytes →
                # 丢弃 + warning（F10 语义，不断连）。
                if isinstance(raw_message, bytes):
                    if "binary_frames" not in self._negotiated:
                        logger.warning("未协商 binary_frames，收到 WS 二进制消息已丢弃")
                        continue
                    # 协议 v2 udp（T4）：帧类型 0x03 = udp_data 走 UDP 会话路径
                    # （另需该连接协商了 udp）；0x01 及其余按 tcp_data 解帧
                    if len(raw_message) >= 2 and raw_message[1] == FRAME_TYPE_UDP_DATA:
                        await self._handle_udp_frame(raw_message)
                        continue
                    try:
                        conn_id, data = decode_tcp_data_frame(raw_message)
                    except ValueError as e:
                        logger.warning(f"丢弃畸形 binary 帧: {e}")
                        continue
                    await self._route_tcp_payload(conn_id, data)
                    continue

                # 热路径解析（轻校验直构；畸形消息走下方异常分支丢弃）
                message = parse_message_fast(raw_message)

                if isinstance(message, PingMessage):
                    # 响应心跳
                    await websocket.send(PongMessage().model_dump_json())

                elif isinstance(message, TunnelRequest):
                    # 处理 HTTP 请求
                    if self._on_request:
                        self._on_request(message)

                    # 执行请求
                    # 对于普通响应，返回 TunnelResponse
                    # 对于 SSE 响应，返回 None（流式消息已在 _execute_request 中发送）
                    response = await self._execute_request(message)
                    if response is not None:
                        await websocket.send(response.model_dump_json())

                elif isinstance(message, TcpConnectMessage):
                    # 处理 TCP 连接建立
                    await self._handle_tcp_connect(message, websocket)

                elif isinstance(message, TcpDataMessage):
                    # 处理 TCP 数据
                    await self._handle_tcp_data(message)

                elif isinstance(message, TcpCloseMessage):
                    # 处理 TCP 连接关闭
                    await self._handle_tcp_close(message)

                elif isinstance(message, UdpOpenMessage):
                    # 处理 UDP 会话建立（协议 v2 udp）
                    await self._handle_udp_open(message)

                elif isinstance(message, UdpCloseMessage):
                    # 处理 UDP 会话关闭
                    await self._handle_udp_close(message)

                else:
                    logger.warning(f"未知消息类型: {type(message)}")

            except json.JSONDecodeError as e:
                logger.error(f"JSON 解析错误: {e}")
            except Exception as e:
                logger.error(f"处理消息错误: {e}", exc_info=True)

    def _is_sse_response(self, headers: dict[str, str]) -> bool:
        """检查是否是 SSE 响应"""
        content_type = headers.get("content-type", "").lower()
        return "text/event-stream" in content_type

    async def _execute_request(self, request: TunnelRequest) -> TunnelResponse | None:
        """
        执行 HTTP 请求

        将隧道请求转发到本地目标服务
        对于 SSE 响应，会发送 StreamStart/StreamChunk/StreamEnd 消息，不返回 TunnelResponse
        对于普通响应，返回 TunnelResponse
        """
        start_time = time.time()

        try:
            # 构建完整 URL（path 先归一化，防止 "@evil/" 改写 authority）
            url = f"{self.config.target_url.rstrip('/')}{normalize_path(request.path)}"

            # 解析请求体
            body = None
            if request.body:
                try:
                    body = json.loads(request.body)
                except json.JSONDecodeError:
                    body = request.body

            # 使用共享客户端的 stream 模式发送请求，以便检测 SSE
            # （连接池跨请求复用；超时在每请求上覆盖：connect 30s，read 用请求超时）
            timeout_config = httpx.Timeout(
                connect=30.0,
                read=float(request.timeout),
                write=30.0,
                pool=30.0,
            )
            async with self._get_http().stream(
                method=request.method,
                url=url,
                headers=request.headers,
                json=body if isinstance(body, (dict, list)) else None,
                content=body if isinstance(body, str) else None,
                timeout=timeout_config,
            ) as response:
                response_headers = dict(response.headers)

                # 检查是否是 SSE 响应
                if self._is_sse_response(response_headers):
                    # SSE 流式响应处理
                    await self._handle_sse_response(
                        request_id=request.id,
                        status=response.status_code,
                        headers=response_headers,
                        response=response,
                        start_time=start_time,
                    )
                    return None  # SSE 响应已通过流式消息发送
                else:
                    # 协议 v2 chunked_http（T3）门控：stream_ok 请求 + 已协商
                    # chunked_http + 阈值 > 0，三者齐备才可能切流式；其余情况
                    # 走既有缓冲路径（行为与 0.7.3 完全一致）
                    stream_eligible = (
                        request.stream_ok
                        and "chunked_http" in self._negotiated
                        and self.config.stream_threshold_bytes > 0
                    )
                    threshold = self.config.stream_threshold_bytes
                    declared_len: int | None = None
                    if stream_eligible:
                        declared_len = self._declared_content_length(response_headers)
                        if declared_len is not None and declared_len > threshold:
                            # Content-Length 已知且超阈值：直接走流式回传
                            await self._stream_large_response(
                                request.id,
                                response.status_code,
                                response_headers,
                                response.aiter_bytes(),
                                start_time,
                            )
                            return None  # 流式消息已发送，不回 TunnelResponse

                    # 普通响应：带内存上限读取（0.7.3，超限 502 并中止；
                    # 对齐生产服务端 cap，避免大响应先把客户端内存打爆）。
                    # Content-Length 未知且允许流式时同样走累积读，
                    # 越过阈值就地切换（已缓冲字节作为首批数据块）
                    cap = self.config.max_response_bytes
                    if cap > 0 or (stream_eligible and declared_len is None):
                        chunks: list[bytes] = []
                        total = 0
                        too_large = False
                        byte_iter = response.aiter_bytes()
                        async for chunk in byte_iter:
                            total += len(chunk)
                            if stream_eligible and total > threshold:
                                # 越线块也是 body 的一部分，一并入首批
                                chunks.append(chunk)
                                await self._stream_large_response(
                                    request.id,
                                    response.status_code,
                                    response_headers,
                                    byte_iter,
                                    start_time,
                                    buffered=chunks,
                                )
                                return None
                            if cap > 0 and total > cap:
                                too_large = True
                                break
                            chunks.append(chunk)
                        if too_large:
                            return TunnelResponse(
                                id=request.id,
                                status=502,
                                error=f"Response too large (> {cap} bytes, client cap)",
                                duration_ms=int((time.time() - start_time) * 1000),
                            )
                        response_body = b"".join(chunks)
                    else:
                        response_body = await response.aread()

                    duration_ms = int((time.time() - start_time) * 1000)

                    return TunnelResponse(
                        id=request.id,
                        status=response.status_code,
                        headers=response_headers,
                        body=response_body.decode("utf-8", errors="replace"),
                        duration_ms=duration_ms,
                    )

        except httpx.TimeoutException:
            duration_ms = int((time.time() - start_time) * 1000)
            return TunnelResponse(
                id=request.id,
                status=504,
                error="Target service timeout",
                duration_ms=duration_ms,
            )
        except httpx.ConnectError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return TunnelResponse(
                id=request.id,
                status=503,
                error=f"Target service unavailable: {e}",
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return TunnelResponse(
                id=request.id,
                status=500,
                error=str(e),
                duration_ms=duration_ms,
            )

    async def _handle_sse_response(
        self,
        request_id: str,
        status: int,
        headers: dict[str, str],
        response: httpx.Response,
        start_time: float,
    ) -> None:
        """
        处理 SSE 流式响应
        
        发送 StreamStart -> StreamChunk* -> StreamEnd 消息
        """
        if not self._websocket:
            logger.error("WebSocket 未连接，无法发送流式响应")
            return

        # 发送 StreamStart
        await self._websocket.send(
            dump_payload(stream_start_payload(request_id, status, headers))
        )
        logger.debug(f"SSE 流开始: request_id={request_id}")

        chunk_count = 0
        error_msg = None

        try:
            # 流式读取并发送数据块（数据面快速路径：手工 dict，wire 键集不变）
            async for chunk in response.aiter_text():
                if chunk:
                    await self._websocket.send(
                        dump_payload(stream_chunk_payload(request_id, chunk, chunk_count))
                    )
                    chunk_count += 1

        except Exception as e:
            error_msg = str(e)
            logger.error(f"SSE 流读取错误: {e}")

        # 发送 StreamEnd
        duration_ms = int((time.time() - start_time) * 1000)
        await self._websocket.send(
            dump_payload(
                stream_end_payload(request_id, error_msg, duration_ms, chunk_count)
            )
        )
        logger.debug(f"SSE 流结束: request_id={request_id}, chunks={chunk_count}, duration={duration_ms}ms")

    # ============== chunked_http：非 SSE 大响应流式回传（协议 v2 T3） ==============

    @staticmethod
    def _declared_content_length(headers: dict[str, str]) -> int | None:
        """解析 Content-Length；缺失/非法按未知长度处理（返回 None）"""
        raw = headers.get("content-length")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_textual_content_type(headers: dict[str, str]) -> bool:
        """块编码判定：text/* 与 application/json 走 UTF-8 明文（plain），
        其余（二进制）走 base64 块（协议 v2 chunked_http，T3）"""
        content_type = (headers.get("content-type") or "").lower()
        return content_type.startswith("text/") or content_type.startswith(
            "application/json"
        )

    async def _stream_large_response(
        self,
        request_id: str,
        status: int,
        headers: dict[str, str],
        source,
        start_time: float,
        buffered: list[bytes] | None = None,
    ) -> None:
        """非 SSE 大响应流式回传（chunked_http）

        发送 StreamStart → StreamChunk* → StreamEnd 后返回（调用方不再回
        TunnelResponse）。块编码按 Content-Type：文本 plain，二进制 base64
        （+33% 局限见 PROTOCOL_V2 §4）。source 为响应体的异步字节迭代器；
        buffered 非空时（Content-Length 未知的就地切换）其拼接内容作为
        首批数据块，随后继续消费 source 剩余部分。
        """
        if not self._websocket:
            logger.error("WebSocket 未连接，无法发送流式响应")
            return

        is_text = self._is_textual_content_type(headers)
        await self._websocket.send(
            dump_payload(stream_start_payload(request_id, status, headers))
        )
        logger.debug(f"大响应流式回传开始: request_id={request_id}")

        chunk_count = 0
        error_msg = None

        async def send_chunk(data: bytes) -> None:
            nonlocal chunk_count
            if not data:
                return
            if is_text:
                payload = stream_chunk_payload(
                    request_id, data.decode("utf-8", errors="replace"), chunk_count
                )
            else:
                payload = stream_chunk_payload(
                    request_id,
                    base64.b64encode(data).decode("ascii"),
                    chunk_count,
                    encoding="base64",
                )
            await self._websocket.send(dump_payload(payload))
            chunk_count += 1

        try:
            if buffered:
                # 就地切换：越过阈值前已缓冲的字节作为首批数据块
                await send_chunk(b"".join(buffered))
            async for chunk in source:
                await send_chunk(chunk)
        except Exception as e:
            error_msg = str(e)
            logger.error(f"流式回传读取错误: {e}")

        duration_ms = int((time.time() - start_time) * 1000)
        await self._websocket.send(
            dump_payload(
                stream_end_payload(request_id, error_msg, duration_ms, chunk_count)
            )
        )
        logger.debug(
            f"大响应流式回传结束: request_id={request_id}, "
            f"chunks={chunk_count}, duration={duration_ms}ms"
        )

    # ============== TCP 模式处理方法 ==============

    async def _handle_tcp_connect(self, message: TcpConnectMessage, websocket) -> None:
        """
        处理 TCP 连接建立请求
        
        创建到目标服务的 TCP 连接
        """
        conn_id = message.conn_id
        logger.info(f"收到 TCP 连接请求: {conn_id}")
        
        # 创建 TCP 连接（读取结束时经 on_closed 回调从连接表移除，F6）
        tcp_conn = TcpConnection(
            conn_id=conn_id,
            target_host=self._target_host,
            target_port=self._target_port,
            websocket=websocket,
            on_closed=self._remove_tcp_connection,
            binary_frames="binary_frames" in self._negotiated,
        )
        
        # 尝试连接
        success = await tcp_conn.connect()
        if success:
            self._tcp_connections[conn_id] = tcp_conn
        else:
            # 连接失败，TcpConnection 已经发送了关闭消息
            logger.warning(f"TCP 连接失败: {conn_id}")

    def _remove_tcp_connection(self, conn: "TcpConnection") -> None:
        """
        TcpConnection 读取结束回调：从连接表移除（F6）

        目标侧先关闭连接时 _read_loop 结束，必须把条目从 _tcp_connections
        移除，否则连接对象与 fd 永久滞留。按对象身份比对，避免移除同 conn_id
        的新连接（服务端重连后可能复用 id 的场景以实际存活的条目为准）。
        """
        if self._tcp_connections.get(conn.conn_id) is conn:
            del self._tcp_connections[conn.conn_id]

    async def _handle_tcp_data(self, message: TcpDataMessage) -> None:
        """
        处理 TCP 数据传输（JSON 形态，base64）

        解 base64 后与 binary_frames 帧路径共用 _route_tcp_payload 落地。
        """
        conn_id = message.conn_id
        conn = self._tcp_connections.get(conn_id)

        if not conn:
            logger.warning(f"收到未知连接的数据: {conn_id}")
            return

        try:
            # 解码 base64 数据
            data = base64.b64decode(message.data)
            await self._route_tcp_payload(conn_id, data, conn=conn)
        except Exception as e:
            logger.error(f"处理 TCP 数据错误: {conn_id}, {e}")
            await conn.close(str(e))

    async def _route_tcp_payload(
        self, conn_id: str, data: bytes, conn: "TcpConnection | None" = None
    ) -> None:
        """
        tcp_data 落地写入（JSON 与 binary 帧两路共用）

        将数据写入对应的 TCP 连接；conn 传入时免二次查表（JSON 路径
        需要连接对象做错误收尾，binary 路径查不到连接按未知连接丢弃）。
        """
        if conn is None:
            conn = self._tcp_connections.get(conn_id)
            if not conn:
                logger.warning(f"收到未知连接的数据: {conn_id}")
                return
        try:
            await conn.write_data(data)
        except Exception as e:
            logger.error(f"处理 TCP 数据错误: {conn_id}, {e}")
            await conn.close(str(e))

    async def _handle_tcp_close(self, message: TcpCloseMessage) -> None:
        """
        处理 TCP 连接关闭
        
        关闭对应的 TCP 连接
        """
        conn_id = message.conn_id
        conn = self._tcp_connections.pop(conn_id, None)
        
        if conn:
            logger.info(f"关闭 TCP 连接: {conn_id}")
            await conn.close(message.error)
        else:
            logger.warning(f"尝试关闭未知连接: {conn_id}")

    # ============== UDP 会话处理方法（协议 v2 udp） ==============

    async def _handle_udp_open(self, message: UdpOpenMessage) -> None:
        """
        处理 UDP 会话建立请求

        建立到目标服务的 UDP socket（每会话一个）并登记；socket 失败时
        回执 udp_close 通知服务端拆会话。
        """
        session_id = message.session_id
        # 能力门控：未协商 udp 的连接不该收到 udp_open（防御：丢弃）
        if "udp" not in self._negotiated:
            logger.warning("未协商 udp，忽略 udp_open")
            return
        if session_id in self._udp_sessions:
            logger.warning(f"UDP 会话已存在，忽略重复的 udp_open: {session_id}")
            return

        session = UdpSession(
            session_id,
            self._websocket,
            on_error=self._teardown_udp_session_on_error,
        )
        if not await session.connect(self._target_host, self._target_port):
            # socket 建立失败：UdpSession 未启动 pump，直接回执关闭
            await self._send_udp_close(session_id, "target socket failed")
            return
        self._udp_sessions[session_id] = session

    async def _handle_udp_frame(self, frame: bytes) -> None:
        """udp_data 二进制帧落地：解帧后写入对应会话的目标 socket"""
        # 能力门控：未协商 udp 的连接不该收到 0x03 帧（防御：丢弃）
        if "udp" not in self._negotiated:
            logger.warning("未协商 udp，收到 udp_data 二进制帧已丢弃")
            return
        try:
            session_id, data = decode_udp_data_frame(frame)
        except ValueError as e:
            logger.warning(f"丢弃畸形 udp 帧: {e}")
            return
        session = self._udp_sessions.get(session_id)
        if session is None:
            logger.debug(f"收到未知 UDP 会话的数据: {session_id}")
            return
        await session.send_to_target(data)

    async def _handle_udp_close(self, message: UdpCloseMessage) -> None:
        """服务端关闭 UDP 会话（空闲超时回收等）：关本地 socket"""
        session_id = message.session_id
        session = self._udp_sessions.pop(session_id, None)
        if session:
            logger.info(
                f"关闭 UDP 会话: {session_id} (reason={message.reason or 'n/a'})"
            )
            await session.close()
        else:
            logger.debug(f"尝试关闭未知 UDP 会话: {session_id}")

    async def _teardown_udp_session_on_error(self, session: UdpSession, exc) -> None:
        """socket 错误收尾：从会话表移除 + 关 socket + 回执 udp_close（尽力而为）"""
        if self._udp_sessions.get(session.session_id) is session:
            del self._udp_sessions[session.session_id]
        await session.close()
        await self._send_udp_close(session.session_id, f"socket error: {exc}")

    async def _send_udp_close(self, session_id: str, reason: str | None = None) -> None:
        """主动上报会话关闭（尽力而为：WS 已断时静默失败）"""
        if not self._websocket:
            return
        try:
            await self._websocket.send(dump_payload(udp_close_payload(session_id, reason)))
        except Exception as e:
            logger.debug(f"发送 udp_close 失败（忽略）: session_id={session_id}, {e}")

    async def _close_all_udp_sessions(self) -> None:
        """回收本连接全部 UDP 会话（WS 断连 / stop() 时调用）"""
        sessions = list(self._udp_sessions.values())
        self._udp_sessions.clear()
        for session in sessions:
            await session.close()
        if sessions:
            logger.info(f"已回收 {len(sessions)} 个 UDP 会话（连接断开）")


async def run_tunnel_client(
    server_url: str,
    token: str,
    target_url: str,
    reconnect_interval: float = 5.0,
) -> None:
    """
    运行隧道客户端

    便捷函数，用于快速启动客户端

    Args:
        server_url: 服务端 WebSocket URL
        token: 隧道令牌
        target_url: 本地目标服务 URL
        reconnect_interval: 重连间隔（秒）
    """
    config = TunnelClientConfig(
        server_url=server_url,
        token=token,
        target_url=target_url,
        reconnect_interval=reconnect_interval,
    )
    client = TunnelClient(config=config)
    await client.run()
