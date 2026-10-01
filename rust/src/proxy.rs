//! 出站 HTTP CONNECT 代理（客户端 → server 的 WS 连接经代理转发）。
//!
//! v1 范围（与 TS 客户端同规格）：
//! - 仅支持 HTTP CONNECT 代理（`http://host:port`，明文隧道）；**SOCKS 明确不支持**，
//!   配置了 socks 会直接报错而不是静默直连；
//! - wss:// 语义：先与代理完成 CONNECT 建立隧道，TLS 在隧道**内部**完成
//!   （`client_async_tls` 对已建立的流做 TLS 握手），端到端加密不变；
//! - 代理只作用于「客户端 → server」的 WS 出站；转发目标（target）流量语义不变。

use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use url::Url;

/// CONNECT 阶段整体超时（TCP 连代理 + 发请求 + 等响应）。
/// 晚高峰跨境线路 RTT 高，10s 足够冗余又不至于让重连退避被拖死。
pub const PROXY_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// 解析代理 URL，返回 (host, port)。
///
/// - `http://host:port`：v1 唯一支持的形态；端口缺省 80；
/// - 无 scheme 的 `host:port` / `host`：按 http 代理处理（宽容输入）；
/// - `socks5://` / `socks5h://` / `socks4://`：报错（v1 明确不支持 SOCKS）；
/// - `https://`（对代理本身走 TLS）：报错（v1 未实现，避免误解为已支持）;
/// - 带 userinfo（`user:pass@`）：报错（v1 不支持代理认证）。
pub fn parse_proxy_url(input: &str) -> Result<(String, u16), String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Err("proxy 配置为空".into());
    }
    let normalized = if trimmed.contains("://") {
        trimmed.to_string()
    } else {
        format!("http://{trimmed}")
    };
    let parsed = Url::parse(&normalized)
        .map_err(|e| format!("proxy URL 无法解析: '{input}' ({e})"))?;

    let host = parsed
        .host_str()
        .ok_or_else(|| format!("proxy URL 缺少 host: '{input}'"))?
        .to_string();
    if host.is_empty() {
        return Err(format!("proxy URL 缺少 host: '{input}'"));
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err(format!(
            "proxy URL 暂不支持代理认证（user:pass@host:port）: '{input}'"
        ));
    }
    match parsed.scheme() {
        "http" => Ok((host, parsed.port().unwrap_or(80))),
        "socks5" | "socks5h" | "socks4" | "socks4a" => Err(format!(
            "proxy 暂不支持 SOCKS（'{input}'）：v1 仅支持 HTTP CONNECT 代理，\
             请改用 http://host:port 形态的代理地址"
        )),
        other => Err(format!(
            "proxy 不支持的 scheme '{other}': '{input}'（v1 仅支持 http://host:port）"
        )),
    }
}

/// 从 server URL（ws:// / wss://）提取 CONNECT 隧道的目标 host:port。
/// 端口缺省按 scheme 补齐：wss → 443，ws → 80。
pub fn server_host_port(server_url: &str) -> Result<(String, u16), String> {
    let parsed = Url::parse(server_url.trim())
        .map_err(|e| format!("server URL 无法解析: '{server_url}' ({e})"))?;
    let host = parsed
        .host_str()
        .filter(|h| !h.is_empty())
        .ok_or_else(|| format!("server URL 缺少 host: '{server_url}'"))?
        .to_string();
    match parsed.scheme() {
        "ws" | "wss" => {
            let default_port = if parsed.scheme() == "wss" { 443 } else { 80 };
            Ok((host, parsed.port().unwrap_or(default_port)))
        }
        other => Err(format!(
            "server URL 应为 ws:// 或 wss://（当前 scheme '{other}'）: '{server_url}'"
        )),
    }
}

/// 组装 CONNECT 请求头（纯函数，单测锁定字节级格式）。
pub fn build_connect_request(host: &str, port: u16) -> String {
    format!("CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n")
}

/// 解析 CONNECT 响应头，2xx 视为隧道建立成功（RFC 7231：代理以 2xx 应答表示
/// 与目标主机的连接已建立）；其余状态码原样带回便于诊断（407 需认证等）。
pub fn parse_connect_response(head: &[u8]) -> Result<(), String> {
    let text = std::str::from_utf8(head)
        .map_err(|_| "CONNECT 响应不是合法 UTF-8".to_string())?;
    let status_line = text
        .split("\r\n")
        .next()
        .unwrap_or("")
        .trim();
    if status_line.is_empty() {
        return Err("CONNECT 响应为空".into());
    }
    let mut parts = status_line.splitn(3, ' ');
    let version = parts.next().unwrap_or("");
    let code = parts
        .next()
        .and_then(|c| c.parse::<u16>().ok())
        .ok_or_else(|| format!("CONNECT 响应状态行无法解析: '{status_line}'"))?;
    let _ = version.starts_with("HTTP/").then_some(());
    if (200..300).contains(&code) {
        Ok(())
    } else {
        Err(format!("代理拒绝 CONNECT（HTTP {code}）: {status_line}"))
    }
}

/// 建立经代理的 CONNECT 隧道：TCP 连代理 → 发 CONNECT → 校验 2xx →
/// 返回已建立隧道的流（调用方在其上直接做 WS 握手 / TLS）。
pub async fn connect_via_proxy(
    proxy_host: &str,
    proxy_port: u16,
    dst_host: &str,
    dst_port: u16,
) -> std::io::Result<TcpStream> {
    let mut stream = TcpStream::connect((proxy_host, proxy_port)).await?;
    let request = build_connect_request(dst_host, dst_port);
    stream.write_all(request.as_bytes()).await?;

    // 逐字节读到头部分隔符：多读的任何字节都属于隧道内数据，而 WS 握手层
    // 不接受预读前缀——逐字节读保证不吞掉隧道首包。头部仅数百字节，开销可忽略。
    let mut head: Vec<u8> = Vec::with_capacity(256);
    let mut byte = [0u8; 1];
    loop {
        let n = stream.read(&mut byte).await?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                format!(
                    "代理在 CONNECT 响应前关闭连接（已读 {} 字节）",
                    head.len()
                ),
            ));
        }
        head.push(byte[0]);
        if head.ends_with(b"\r\n\r\n") {
            break;
        }
        if head.len() > 16 * 1024 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "CONNECT 响应头超过 16KB",
            ));
        }
    }
    parse_connect_response(&head).map_err(|e| std::io::Error::new(
        std::io::ErrorKind::Other,
        e,
    ))?;
    Ok(stream)
}

#[cfg(test)]
mod tests {
    use super::*;

    // ============== parse_proxy_url ==============

    #[test]
    fn parse_proxy_url_http_with_port() {
        assert_eq!(
            parse_proxy_url("http://127.0.0.1:7890").unwrap(),
            ("127.0.0.1".into(), 7890)
        );
    }

    #[test]
    fn parse_proxy_url_default_port_80() {
        assert_eq!(
            parse_proxy_url("http://proxy.corp.example").unwrap(),
            ("proxy.corp.example".into(), 80)
        );
    }

    #[test]
    fn parse_proxy_url_bare_host_port_defaults_to_http() {
        // 无 scheme 的宽容输入：按 http 代理处理
        assert_eq!(
            parse_proxy_url("127.0.0.1:7890").unwrap(),
            ("127.0.0.1".into(), 7890)
        );
        assert_eq!(
            parse_proxy_url("localhost").unwrap(),
            ("localhost".into(), 80)
        );
    }

    #[test]
    fn parse_proxy_url_trims_whitespace() {
        assert_eq!(
            parse_proxy_url("  http://127.0.0.1:7890  ").unwrap(),
            ("127.0.0.1".into(), 7890)
        );
    }

    #[test]
    fn parse_proxy_url_ipv6_host() {
        assert_eq!(
            parse_proxy_url("http://[::1]:7897").unwrap(),
            ("[::1]".into(), 7897)
        );
    }

    #[test]
    fn parse_proxy_url_rejects_socks() {
        for input in ["socks5://127.0.0.1:1080", "socks5h://p:1080", "socks4://p:1080"] {
            let err = parse_proxy_url(input).unwrap_err();
            assert!(err.contains("SOCKS"), "{input} -> {err}");
        }
    }

    #[test]
    fn parse_proxy_url_rejects_https_proxy() {
        let err = parse_proxy_url("https://proxy:8443").unwrap_err();
        assert!(err.contains("http://host:port"), "{err}");
    }

    #[test]
    fn parse_proxy_url_rejects_userinfo() {
        let err = parse_proxy_url("http://user:pass@proxy:8080").unwrap_err();
        assert!(err.contains("认证"), "{err}");
    }

    #[test]
    fn parse_proxy_url_rejects_empty_and_garbage() {
        assert!(parse_proxy_url("").is_err());
        assert!(parse_proxy_url("   ").is_err());
        assert!(parse_proxy_url("http://").is_err());
    }

    // ============== server_host_port ==============

    #[test]
    fn server_host_port_extracts_explicit_port() {
        assert_eq!(
            server_host_port("ws://myhost:9000/ws/tunnel").unwrap(),
            ("myhost".into(), 9000)
        );
    }

    #[test]
    fn server_host_port_defaults_by_scheme() {
        assert_eq!(
            server_host_port("wss://tun.example.com/ws/tunnel").unwrap(),
            ("tun.example.com".into(), 443)
        );
        assert_eq!(
            server_host_port("ws://1.2.3.4/ws/tunnel").unwrap(),
            ("1.2.3.4".into(), 80)
        );
    }

    #[test]
    fn server_host_port_rejects_non_ws_scheme() {
        let err = server_host_port("https://example.com/ws/tunnel").unwrap_err();
        assert!(err.contains("ws://"), "{err}");
    }

    #[test]
    fn server_host_port_rejects_garbage() {
        assert!(server_host_port("not a url").is_err());
    }

    // ============== build_connect_request ==============

    #[test]
    fn connect_request_byte_format() {
        assert_eq!(
            build_connect_request("srv.example.com", 443),
            "CONNECT srv.example.com:443 HTTP/1.1\r\nHost: srv.example.com:443\r\n\r\n"
        );
        assert_eq!(
            build_connect_request("127.0.0.1", 8000),
            "CONNECT 127.0.0.1:8000 HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n\r\n"
        );
    }

    // ============== parse_connect_response ==============

    #[test]
    fn connect_response_200_ok() {
        assert!(parse_connect_response(b"HTTP/1.1 200 Connection established\r\n\r\n").is_ok());
        // 常见代理（如 clash/squid）也用 HTTP/1.0 应答
        assert!(parse_connect_response(b"HTTP/1.0 200 OK\r\n\r\n").is_ok());
        // 204 No Content 同属 2xx，语义上隧道已建立
        assert!(parse_connect_response(b"HTTP/1.1 204 No Content\r\n\r\n").is_ok());
    }

    #[test]
    fn connect_response_407_reports_status() {
        let err = parse_connect_response(
            b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic\r\n\r\n",
        )
        .unwrap_err();
        assert!(err.contains("407"), "{err}");
    }

    #[test]
    fn connect_response_non_2xx_rejected() {
        assert!(parse_connect_response(b"HTTP/1.1 502 Bad Gateway\r\n\r\n").is_err());
        assert!(parse_connect_response(b"HTTP/1.1 302 Found\r\n\r\n").is_err());
    }

    #[test]
    fn connect_response_garbage_rejected() {
        assert!(parse_connect_response(b"garbage\r\n\r\n").is_err());
        assert!(parse_connect_response(b"").is_err());
        assert!(parse_connect_response("HTTP/1.1 200 中文\r\n\r\n".as_bytes()).is_ok());
    }
}
