"""
协议测试
"""

import pytest
from tunely.protocol import (
    MessageType,
    AuthMessage,
    AuthOkMessage,
    AuthErrorMessage,
    TunnelRequest,
    TunnelResponse,
    PingMessage,
    PongMessage,
    StreamStartMessage,
    StreamChunkMessage,
    StreamEndMessage,
    parse_message,
)


class TestMessageTypes:
    """测试消息类型"""

    def test_auth_message(self):
        """测试认证消息"""
        msg = AuthMessage(token="test_token")
        assert msg.type == MessageType.AUTH
        assert msg.token == "test_token"
        assert msg.client_version == "0.1.0"

    def test_auth_ok_message(self):
        """测试认证成功消息"""
        msg = AuthOkMessage(domain="test-domain", tunnel_id="123")
        assert msg.type == MessageType.AUTH_OK
        assert msg.domain == "test-domain"
        assert msg.tunnel_id == "123"

    def test_auth_error_message(self):
        """测试认证失败消息"""
        msg = AuthErrorMessage(error="Invalid token")
        assert msg.type == MessageType.AUTH_ERROR
        assert msg.error == "Invalid token"

    def test_tunnel_request(self):
        """测试隧道请求"""
        msg = TunnelRequest(
            id="req-001",
            method="POST",
            path="/api/chat",
            headers={"Content-Type": "application/json"},
            body='{"message": "hello"}',
        )
        assert msg.type == MessageType.REQUEST
        assert msg.id == "req-001"
        assert msg.method == "POST"
        assert msg.path == "/api/chat"

    def test_tunnel_response(self):
        """测试隧道响应"""
        msg = TunnelResponse(
            id="req-001",
            status=200,
            headers={"Content-Type": "application/json"},
            body='{"response": "hi"}',
            duration_ms=100,
        )
        assert msg.type == MessageType.RESPONSE
        assert msg.id == "req-001"
        assert msg.status == 200
        assert msg.duration_ms == 100

    def test_ping_pong_messages(self):
        """测试心跳消息"""
        ping = PingMessage()
        pong = PongMessage()
        assert ping.type == MessageType.PING
        assert pong.type == MessageType.PONG


class TestStreamMessages:
    """测试流式响应消息（SSE 支持）"""

    def test_stream_start_message(self):
        """测试流开始消息"""
        msg = StreamStartMessage(
            id="req-001",
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        assert msg.type == MessageType.STREAM_START
        assert msg.id == "req-001"
        assert msg.status == 200
        assert msg.headers["Content-Type"] == "text/event-stream"

    def test_stream_chunk_message(self):
        """测试流数据块消息"""
        msg = StreamChunkMessage(
            id="req-001",
            data="data: hello world\n\n",
            sequence=0,
        )
        assert msg.type == MessageType.STREAM_CHUNK
        assert msg.id == "req-001"
        assert msg.data == "data: hello world\n\n"
        assert msg.sequence == 0

    def test_stream_end_message(self):
        """测试流结束消息"""
        msg = StreamEndMessage(
            id="req-001",
            duration_ms=1500,
            total_chunks=10,
        )
        assert msg.type == MessageType.STREAM_END
        assert msg.id == "req-001"
        assert msg.duration_ms == 1500
        assert msg.total_chunks == 10
        assert msg.error is None

    def test_stream_end_with_error(self):
        """测试带错误的流结束消息"""
        msg = StreamEndMessage(
            id="req-001",
            error="Connection reset",
            duration_ms=500,
            total_chunks=5,
        )
        assert msg.type == MessageType.STREAM_END
        assert msg.error == "Connection reset"


class TestParseMessage:
    """测试消息解析"""

    def test_parse_auth_message(self):
        """解析认证消息"""
        data = {"type": "auth", "token": "test123"}
        msg = parse_message(data)
        assert isinstance(msg, AuthMessage)
        assert msg.token == "test123"

    def test_parse_request_message(self):
        """解析请求消息"""
        data = {
            "type": "request",
            "id": "req-001",
            "method": "GET",
            "path": "/api/test",
            "headers": {},
        }
        msg = parse_message(data)
        assert isinstance(msg, TunnelRequest)
        assert msg.id == "req-001"

    def test_parse_response_message(self):
        """解析响应消息"""
        data = {
            "type": "response",
            "id": "req-001",
            "status": 200,
            "headers": {},
        }
        msg = parse_message(data)
        assert isinstance(msg, TunnelResponse)
        assert msg.status == 200

    def test_parse_unknown_type(self):
        """解析未知消息类型"""
        data = {"type": "unknown"}
        with pytest.raises(ValueError):
            parse_message(data)

    def test_parse_stream_start(self):
        """解析流开始消息"""
        data = {
            "type": "stream_start",
            "id": "req-001",
            "status": 200,
            "headers": {"Content-Type": "text/event-stream"},
        }
        msg = parse_message(data)
        assert isinstance(msg, StreamStartMessage)
        assert msg.status == 200

    def test_parse_stream_chunk(self):
        """解析流数据块消息"""
        data = {
            "type": "stream_chunk",
            "id": "req-001",
            "data": "data: test\n\n",
            "sequence": 0,
        }
        msg = parse_message(data)
        assert isinstance(msg, StreamChunkMessage)
        assert msg.data == "data: test\n\n"

    def test_parse_stream_end(self):
        """解析流结束消息"""
        data = {
            "type": "stream_end",
            "id": "req-001",
            "duration_ms": 1000,
            "total_chunks": 5,
        }
        msg = parse_message(data)
        assert isinstance(msg, StreamEndMessage)
        assert msg.total_chunks == 5


class TestMessageSerialization:
    """测试消息序列化"""

    def test_auth_message_json(self):
        """认证消息序列化"""
        msg = AuthMessage(token="test_token")
        json_str = msg.model_dump_json()
        assert "test_token" in json_str
        assert "auth" in json_str

    def test_tunnel_request_json(self):
        """隧道请求序列化"""
        msg = TunnelRequest(
            id="req-001",
            method="POST",
            path="/api/chat",
            headers={"Content-Type": "application/json"},
            body='{"message": "hello"}',
        )
        json_str = msg.model_dump_json()
        assert "req-001" in json_str
        assert "POST" in json_str
