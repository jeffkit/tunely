"""监听原生 TLS 单元测试（0.11，docs/MIGRATION_TCP_ONLY.md §5）

覆盖：
- 未配置证书/私钥 → 明文（builder 返回 None，零行为变化）
- 配置不完整（缺 key 等）→ RuntimeError 可读报错，不静默降级明文
- ALPN 锁：客户端同时提供 h2 与 http/1.1，服务端必须选中 http/1.1
  （TLS 终止后字节原样入隧道，协商出 h2 而目标只讲 HTTP/1.1 即断连）
- ALPN 配置可覆盖（'none' / 自定义列表）
"""

import asyncio
import ssl

import pytest

from tunely.config import TunnelServerConfig
from tunely.server import TunnelServer


def _make_server(**config_kwargs) -> TunnelServer:
    return TunnelServer(
        config=TunnelServerConfig(
            database_url="sqlite+aiosqlite:///:memory:", **config_kwargs
        )
    )


@pytest.fixture(scope="module")
def self_signed(tmp_path_factory):
    """现造自签证书（依赖 cryptography，缺失则整模块 skip）"""
    cryptography = pytest.importorskip("cryptography")
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
    d = tmp_path_factory.mktemp("tls")
    cert_path = d / "cert.pem"
    key_path = d / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


class TestBuildListenerSslContext:
    def test_no_config_returns_none(self):
        """未配置 → 明文监听（零行为变化）"""
        assert _make_server()._build_listener_ssl_context() is None

    def test_cert_only_raises(self, self_signed):
        """只给 cert 不给 key → 可读报错，不静默降级明文"""
        cert_path, _ = self_signed
        srv = _make_server(listener_tls_cert_file=cert_path)
        with pytest.raises(RuntimeError, match="成对"):
            srv._build_listener_ssl_context()

    def test_missing_key_file_raises(self, self_signed):
        cert_path, _ = self_signed
        srv = _make_server(
            listener_tls_cert_file=cert_path,
            listener_tls_key_file="/nonexistent/key.pem",
        )
        with pytest.raises(RuntimeError, match="加载监听 TLS"):
            srv._build_listener_ssl_context()

    def test_build_returns_context_with_default_alpn(self, self_signed):
        cert_path, key_path = self_signed
        srv = _make_server(
            listener_tls_cert_file=cert_path,
            listener_tls_key_file=key_path,
        )
        ctx = srv._build_listener_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        # 默认 ALPN 恒锁 http/1.1（双端握手时验证，见下）
        assert ctx is not None


class TestAlpnLockHandshake:
    @pytest.mark.asyncio
    async def test_client_offering_h2_selects_http11(self, self_signed):
        """客户端同时提供 h2 与 http/1.1 → 服务端必须选 http/1.1"""
        cert_path, key_path = self_signed
        srv = _make_server(
            listener_tls_cert_file=cert_path,
            listener_tls_key_file=key_path,
        )
        ctx = srv._build_listener_ssl_context()

        async def handler(reader, writer):
            try:
                await reader.read(1024)
            finally:
                writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ctx)
        port = server.sockets[0].getsockname()[1]
        try:
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE
            client_ctx.set_alpn_protocols(["h2", "http/1.1"])
            _reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=client_ctx, server_hostname="localhost")
            selected = writer.get_extra_info("ssl_object").selected_alpn_protocol()
            writer.close()
            assert selected == "http/1.1", f"ALPN 应锁 http/1.1，实际 {selected!r}"
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_alpn_config_override(self, self_signed):
        """ALPN 配置可覆盖：'none' 不做 ALPN（selected 为 None）；自定义列表生效"""
        cert_path, key_path = self_signed

        async def handshake(alpn_config: str | None) -> str | None:
            kwargs = dict(
                listener_tls_cert_file=cert_path,
                listener_tls_key_file=key_path,
            )
            if alpn_config is not None:
                kwargs["listener_tls_alpn"] = alpn_config
            ctx = _make_server(**kwargs)._build_listener_ssl_context()

            async def handler(reader, writer):
                try:
                    await reader.read(1024)
                finally:
                    writer.close()

            server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ctx)
            port = server.sockets[0].getsockname()[1]
            try:
                client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                client_ctx.check_hostname = False
                client_ctx.verify_mode = ssl.CERT_NONE
                client_ctx.set_alpn_protocols(["h2", "http/1.1"])
                _reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port, ssl=client_ctx, server_hostname="localhost")
                try:
                    return writer.get_extra_info("ssl_object").selected_alpn_protocol()
                finally:
                    writer.close()
            finally:
                server.close()
                await server.wait_closed()

        # 'none' = 不做 ALPN：客户端提供了协议列表但服务端不选（None）
        assert await handshake("none") is None
        # 自定义列表：服务端按列表偏好选 h2
        assert await handshake("h2, http/1.1") == "h2"
