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
import json
import logging
import time
from datetime import datetime
from typing import Callable

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
    parse_message,
)

logger = logging.getLogger(__name__)


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

        # 回调函数
        self._on_connect: Callable[[], None] | None = None
        self._on_disconnect: Callable[[], None] | None = None
        self._on_request: Callable[[TunnelRequest], None] | None = None

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
                    logger.error(f"超过最大重连次数 ({max_attempts})，停止")
                    break

                logger.warning(
                    f"连接断开: {e}，{self.config.reconnect_interval}秒后重连 "
                    f"(第 {self._reconnect_count} 次)"
                )
                await asyncio.sleep(self.config.reconnect_interval)

    async def stop(self) -> None:
        """停止客户端"""
        self._running = False
        if self._websocket:
            await self._websocket.close()

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
                force=self.config.force,
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
                self._connected = True
                self._reconnect_count = 0

                logger.info(f"已连接: domain={self._domain}")

                if self._on_connect:
                    self._on_connect()

                # 消息循环
                await self._message_loop(websocket)

    async def _message_loop(self, websocket) -> None:
        """消息处理循环"""
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
                message = parse_message(data)

                if isinstance(message, PingMessage):
                    # 响应心跳
                    await websocket.send(PongMessage().model_dump_json())

                elif isinstance(message, TunnelRequest):
                    # 处理请求
                    if self._on_request:
                        self._on_request(message)

                    # 执行请求
                    # 对于普通响应，返回 TunnelResponse
                    # 对于 SSE 响应，返回 None（流式消息已在 _execute_request 中发送）
                    response = await self._execute_request(message)
                    if response is not None:
                        await websocket.send(response.model_dump_json())

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
            # 构建完整 URL
            url = f"{self.config.target_url.rstrip('/')}{request.path}"

            # 解析请求体
            body = None
            if request.body:
                try:
                    body = json.loads(request.body)
                except json.JSONDecodeError:
                    body = request.body

            # 使用 stream 模式发送请求，以便检测 SSE
            # 配置超时：connect 30秒，read 使用请求的超时时间，write 30秒
            timeout_config = httpx.Timeout(
                connect=30.0,
                read=float(request.timeout),
                write=30.0,
                pool=30.0,
            )
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                async with client.stream(
                    method=request.method,
                    url=url,
                    headers=request.headers,
                    json=body if isinstance(body, (dict, list)) else None,
                    content=body if isinstance(body, str) else None,
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
                        # 普通响应：读取完整内容
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
        start_msg = StreamStartMessage(
            id=request_id,
            status=status,
            headers=headers,
        )
        await self._websocket.send(start_msg.model_dump_json())
        logger.debug(f"SSE 流开始: request_id={request_id}")

        chunk_count = 0
        error_msg = None

        try:
            # 流式读取并发送数据块
            async for chunk in response.aiter_text():
                if chunk:
                    chunk_msg = StreamChunkMessage(
                        id=request_id,
                        data=chunk,
                        sequence=chunk_count,
                    )
                    await self._websocket.send(chunk_msg.model_dump_json())
                    chunk_count += 1

        except Exception as e:
            error_msg = str(e)
            logger.error(f"SSE 流读取错误: {e}")

        # 发送 StreamEnd
        duration_ms = int((time.time() - start_time) * 1000)
        end_msg = StreamEndMessage(
            id=request_id,
            error=error_msg,
            duration_ms=duration_ms,
            total_chunks=chunk_count,
        )
        await self._websocket.send(end_msg.model_dump_json())
        logger.debug(f"SSE 流结束: request_id={request_id}, chunks={chunk_count}, duration={duration_ms}ms")


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
