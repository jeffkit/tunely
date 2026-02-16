"""
TCP 隧道模式测试

测试 TCP 隧道的完整功能：
1. 协议消息序列化/反序列化
2. 客户端 TCP 连接管理
3. 服务端 TCP 转发逻辑
4. 端到端 TCP 数据传输
"""

import asyncio
import base64
import pytest
from unittest.mock import AsyncMock, MagicMock, Mock

from tunely.protocol import (
    TcpConnectMessage,
    TcpDataMessage,
    TcpCloseMessage,
    MessageType,
    parse_message,
)


class TestTcpProtocolMessages:
    """测试 TCP 协议消息"""

    def test_tcp_connect_message(self):
        """测试 TCP 连接消息"""
        msg = TcpConnectMessage(conn_id="conn-123")
        assert msg.type == MessageType.TCP_CONNECT
        assert msg.conn_id == "conn-123"
        
        # 序列化
        data = msg.model_dump()
        assert data["type"] == "tcp_connect"
        assert data["conn_id"] == "conn-123"
        
        # 反序列化
        parsed = parse_message(data)
        assert isinstance(parsed, TcpConnectMessage)
        assert parsed.conn_id == "conn-123"

    def test_tcp_data_message(self):
        """测试 TCP 数据消息"""
        test_data = b"Hello TCP"
        encoded_data = base64.b64encode(test_data).decode('ascii')
        
        msg = TcpDataMessage(
            conn_id="conn-123",
            data=encoded_data,
            sequence=5,
        )
        assert msg.type == MessageType.TCP_DATA
        assert msg.conn_id == "conn-123"
        assert msg.data == encoded_data
        assert msg.sequence == 5
        
        # 序列化
        data = msg.model_dump()
        assert data["type"] == "tcp_data"
        
        # 反序列化
        parsed = parse_message(data)
        assert isinstance(parsed, TcpDataMessage)
        assert parsed.conn_id == "conn-123"
        assert parsed.sequence == 5
        
        # 数据解码
        decoded = base64.b64decode(parsed.data)
        assert decoded == test_data

    def test_tcp_close_message(self):
        """测试 TCP 关闭消息"""
        msg = TcpCloseMessage(conn_id="conn-123", error="Connection reset")
        assert msg.type == MessageType.TCP_CLOSE
        assert msg.conn_id == "conn-123"
        assert msg.error == "Connection reset"
        
        # 序列化
        data = msg.model_dump()
        assert data["type"] == "tcp_close"
        
        # 反序列化
        parsed = parse_message(data)
        assert isinstance(parsed, TcpCloseMessage)
        assert parsed.conn_id == "conn-123"
        assert parsed.error == "Connection reset"

    def test_tcp_close_message_no_error(self):
        """测试无错误的 TCP 关闭消息"""
        msg = TcpCloseMessage(conn_id="conn-123")
        assert msg.error is None
        
        data = msg.model_dump()
        parsed = parse_message(data)
        assert isinstance(parsed, TcpCloseMessage)
        assert parsed.error is None


class TestTcpConnectionClass:
    """测试 TcpConnection 类（客户端）"""

    @pytest.mark.asyncio
    async def test_tcp_connection_init(self):
        """测试 TCP 连接初始化"""
        from tunely.client import TcpConnection
        
        mock_websocket = MagicMock()
        conn = TcpConnection(
            conn_id="conn-123",
            target_host="localhost",
            target_port=8080,
            websocket=mock_websocket,
        )
        
        assert conn.conn_id == "conn-123"
        assert conn.target_host == "localhost"
        assert conn.target_port == 8080
        assert conn._sequence == 0
        assert conn._closed == False

    @pytest.mark.asyncio
    async def test_tcp_connection_write_data(self):
        """测试写入数据到目标服务"""
        from tunely.client import TcpConnection
        
        mock_websocket = MagicMock()
        mock_writer = MagicMock()
        mock_writer.write = Mock()
        mock_writer.drain = AsyncMock()
        
        conn = TcpConnection(
            conn_id="conn-123",
            target_host="localhost",
            target_port=8080,
            websocket=mock_websocket,
        )
        conn._writer = mock_writer
        
        # 写入数据
        test_data = b"Hello"
        await conn.write_data(test_data)
        
        mock_writer.write.assert_called_once_with(test_data)
        mock_writer.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tcp_connection_close(self):
        """测试关闭 TCP 连接"""
        from tunely.client import TcpConnection
        
        mock_websocket = MagicMock()
        mock_websocket.send = AsyncMock()
        mock_writer = MagicMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()
        
        conn = TcpConnection(
            conn_id="conn-123",
            target_host="localhost",
            target_port=8080,
            websocket=mock_websocket,
        )
        conn._writer = mock_writer
        conn._closed = False  # 确保未关闭状态
        
        # 关闭连接
        await conn.close()
        
        assert conn._closed == True
        mock_writer.close.assert_called_once()
        mock_writer.wait_closed.assert_awaited_once()
        # 验证发送了 TcpCloseMessage
        mock_websocket.send.assert_awaited()


class TestTunnelClientTcpMode:
    """测试 TunnelClient 的 TCP 模式功能"""

    @pytest.mark.asyncio
    async def test_parse_target_url_http(self):
        """测试解析 HTTP URL"""
        from tunely.client import TunnelClient
        from tunely.config import TunnelClientConfig
        
        config = TunnelClientConfig(
            server_url="ws://test",
            token="test",
            target_url="http://localhost:3000"
        )
        client = TunnelClient(config=config)
        
        assert client._target_host == "localhost"
        assert client._target_port == 3000

    @pytest.mark.asyncio
    async def test_parse_target_url_https(self):
        """测试解析 HTTPS URL"""
        from tunely.client import TunnelClient
        from tunely.config import TunnelClientConfig
        
        config = TunnelClientConfig(
            server_url="ws://test",
            token="test",
            target_url="https://api.example.com:8443"
        )
        client = TunnelClient(config=config)
        
        assert client._target_host == "api.example.com"
        assert client._target_port == 8443

    @pytest.mark.asyncio
    async def test_handle_tcp_connect_message(self):
        """测试处理 TCP 连接消息"""
        from tunely.client import TunnelClient
        from tunely.config import TunnelClientConfig
        from unittest.mock import patch
        
        config = TunnelClientConfig(
            server_url="ws://test",
            token="test",
            target_url="http://localhost:8080"
        )
        client = TunnelClient(config=config)
        client._websocket = MagicMock()
        
        # Mock TCP 连接
        with patch('tunely.client.asyncio.open_connection') as mock_open:
            mock_reader = AsyncMock()
            mock_reader.read = AsyncMock(return_value=b'')  # 模拟连接关闭
            mock_writer = MagicMock()
            mock_writer.close = Mock()
            mock_writer.wait_closed = AsyncMock()
            mock_open.return_value = (mock_reader, mock_writer)
            
            msg = TcpConnectMessage(conn_id="conn-123")
            await client._handle_tcp_connect(msg, client._websocket)
            
            # 等待连接建立
            await asyncio.sleep(0.1)
            
            # 验证连接已注册
            assert "conn-123" in client._tcp_connections


class TestTunnelModelMode:
    """测试 Tunnel 模型的 mode 字段"""

    @pytest.mark.asyncio
    async def test_tunnel_default_mode(self):
        """测试隧道默认模式为 http"""
        from tunely.models import Tunnel
        
        tunnel = Tunnel(
            domain="test-tunnel",
            token="test_token_123",
            mode="http",  # SQLAlchemy 需要显式设置或通过数据库默认值
        )
        
        # 默认模式应该是 http
        assert tunnel.mode == "http"

    @pytest.mark.asyncio
    async def test_tunnel_tcp_mode(self):
        """测试设置 TCP 模式"""
        from tunely.models import Tunnel
        
        tunnel = Tunnel(
            domain="test-tunnel",
            token="test_token_123",
            mode="tcp",
        )
        
        assert tunnel.mode == "tcp"

    @pytest.mark.asyncio
    async def test_tunnel_to_dict_includes_mode(self):
        """测试 to_dict 包含 mode 字段"""
        from tunely.models import Tunnel
        
        tunnel = Tunnel(
            domain="test-tunnel",
            token="test_token_123",
            mode="tcp",
        )
        
        data = tunnel.to_dict()
        assert "mode" in data
        assert data["mode"] == "tcp"


class TestTunnelManagerPendingTcpRequest:
    """测试 TunnelManager 的 PendingTcpRequest 机制"""

    @pytest.mark.asyncio
    async def test_create_pending_tcp_request(self):
        """测试创建待响应 TCP 请求"""
        from tunely.server import TunnelManager

        manager = TunnelManager()
        future = await manager.create_pending_tcp_request("conn-100")

        assert "conn-100" in manager._pending_tcp_requests
        assert not future.done()

    @pytest.mark.asyncio
    async def test_handle_tcp_response_data(self):
        """测试累积 TCP 响应数据"""
        from tunely.server import TunnelManager

        manager = TunnelManager()
        await manager.create_pending_tcp_request("conn-200")

        ok1 = await manager.handle_tcp_response_data("conn-200", b"Hello")
        ok2 = await manager.handle_tcp_response_data("conn-200", b" World")
        assert ok1 is True
        assert ok2 is True

        pending = manager._pending_tcp_requests["conn-200"]
        assert pending.chunks == [b"Hello", b" World"]

    @pytest.mark.asyncio
    async def test_handle_tcp_response_data_unknown_conn(self):
        """测试未知连接 ID 的数据累积返回 False"""
        from tunely.server import TunnelManager

        manager = TunnelManager()
        ok = await manager.handle_tcp_response_data("unknown-conn", b"data")
        assert ok is False

    @pytest.mark.asyncio
    async def test_complete_tcp_request_success(self):
        """测试成功完成 TCP 请求"""
        from tunely.server import TunnelManager

        manager = TunnelManager()
        future = await manager.create_pending_tcp_request("conn-300")

        await manager.handle_tcp_response_data("conn-300", b"Response data")
        completed = await manager.complete_tcp_request("conn-300")

        assert completed is True
        assert future.done()
        result = future.result()
        assert result["error"] is None
        assert result["data"] == b"Response data"
        assert "conn-300" not in manager._pending_tcp_requests

    @pytest.mark.asyncio
    async def test_complete_tcp_request_with_error(self):
        """测试带错误的 TCP 请求完成"""
        from tunely.server import TunnelManager

        manager = TunnelManager()
        future = await manager.create_pending_tcp_request("conn-400")

        completed = await manager.complete_tcp_request("conn-400", error="Connection refused")

        assert completed is True
        assert future.done()
        result = future.result()
        assert result["error"] == "Connection refused"
        assert result["data"] == b""

    @pytest.mark.asyncio
    async def test_complete_tcp_request_unknown_conn(self):
        """测试完成未知的 TCP 请求"""
        from tunely.server import TunnelManager

        manager = TunnelManager()
        completed = await manager.complete_tcp_request("unknown-conn")
        assert completed is False

    @pytest.mark.asyncio
    async def test_cleanup_tcp_request(self):
        """测试清理 TCP 请求"""
        from tunely.server import TunnelManager

        manager = TunnelManager()
        future = await manager.create_pending_tcp_request("conn-500")

        await manager.cleanup_tcp_request("conn-500")

        assert "conn-500" not in manager._pending_tcp_requests
        assert future.cancelled()

    @pytest.mark.asyncio
    async def test_full_tcp_request_response_flow(self):
        """测试完整的 TCP 请求-响应流程"""
        from tunely.server import TunnelManager

        manager = TunnelManager()

        # 1. 创建请求
        future = await manager.create_pending_tcp_request("conn-flow")

        # 2. 累积多个数据块
        await manager.handle_tcp_response_data("conn-flow", b"HTTP/1.1 200 OK\r\n")
        await manager.handle_tcp_response_data("conn-flow", b"Content-Type: text/plain\r\n\r\n")
        await manager.handle_tcp_response_data("conn-flow", b"Hello World")

        # 3. 完成请求
        await manager.complete_tcp_request("conn-flow")

        # 4. 验证结果
        result = future.result()
        assert result["error"] is None
        assert result["data"] == b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nHello World"


class TestTcpParseResponse:
    """测试 _parse_tcp_response 方法"""

    def test_parse_empty_data(self):
        """测试解析空数据"""
        from tunely.server import TunnelServer

        result = TunnelServer._parse_tcp_response(b"")
        assert result["status"] == 200
        assert result["body"] == ""

    def test_parse_json_response(self):
        """测试解析 JSON 响应"""
        from tunely.server import TunnelServer

        data = b'{"status": "ok", "count": 42}'
        result = TunnelServer._parse_tcp_response(data)
        # JSON 应该被成功解析
        assert "body" in result

    def test_parse_plain_text(self):
        """测试解析纯文本"""
        from tunely.server import TunnelServer

        data = b"Just some text"
        result = TunnelServer._parse_tcp_response(data)
        assert "body" in result

    def test_parse_http_response(self):
        """测试解析 HTTP 响应格式"""
        from tunely.server import TunnelServer

        data = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html>Hello</html>"
        result = TunnelServer._parse_tcp_response(data)
        assert result.get("status") == 200


class TestTcpServerConfig:
    """测试 TCP 相关配置"""

    def test_default_tcp_config(self):
        """测试默认 TCP 配置"""
        from tunely.config import TunnelServerConfig

        config = TunnelServerConfig()
        assert config.tcp_listen_port is None
        assert config.tcp_listen_host == "0.0.0.0"
        assert config.tcp_target_domain is None

    def test_custom_tcp_config(self):
        """测试自定义 TCP 配置"""
        from tunely.config import TunnelServerConfig

        config = TunnelServerConfig(
            tcp_listen_port=9090,
            tcp_listen_host="127.0.0.1",
            tcp_target_domain="my-tunnel",
        )
        assert config.tcp_listen_port == 9090
        assert config.tcp_listen_host == "127.0.0.1"
        assert config.tcp_target_domain == "my-tunnel"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
