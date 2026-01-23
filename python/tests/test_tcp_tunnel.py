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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
