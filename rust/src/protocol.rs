//! WS-Tunnel 协议定义（wire 1.1：SSE + TCP 模式）
//!
//! 字段名与 python/tunely/protocol.py、typescript/src/protocol.ts 逐一对齐。
//! `timestamp` 等纯元数据字段本实现省略（协议中均为可选，服务端不强依赖）。

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// 消息类型，取值与服务端一致（snake_case）
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MessageType {
    Auth,
    AuthOk,
    AuthError,
    Request,
    Response,
    StreamStart,
    StreamChunk,
    StreamEnd,
    TcpConnect,
    TcpData,
    TcpClose,
    Ping,
    Pong,
}

/// 客户端版本标识（服务端仅记录）
pub const CLIENT_VERSION: &str = env!("CARGO_PKG_VERSION");

fn is_zero(n: &u32) -> bool {
    *n == 0
}

fn is_zero_u64(n: &u64) -> bool {
    *n == 0
}

/// wire 协议消息（tag = "type"，变体名转 snake_case 后即线上取值）
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Message {
    /// 客户端认证请求
    Auth {
        token: String,
        client_version: String,
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        force: bool,
    },
    /// 认证成功
    AuthOk {
        domain: String,
        tunnel_id: String,
        #[serde(default)]
        server_version: Option<String>,
    },
    /// 认证失败
    AuthError {
        error: String,
        #[serde(default)]
        code: Option<String>,
    },

    /// HTTP 请求（服务端 → 客户端）
    Request {
        id: String,
        method: String,
        path: String,
        #[serde(default)]
        headers: HashMap<String, String>,
        #[serde(default)]
        body: Option<String>,
        /// 秒
        #[serde(default)]
        timeout: Option<f64>,
    },
    /// HTTP 响应（客户端 → 服务端）
    Response {
        id: String,
        status: u16,
        #[serde(default)]
        headers: HashMap<String, String>,
        #[serde(default)]
        body: Option<String>,
        #[serde(default)]
        error: Option<String>,
        #[serde(default, skip_serializing_if = "is_zero_u64")]
        duration_ms: u64,
    },

    /// SSE 流式开始
    StreamStart {
        id: String,
        status: u16,
        #[serde(default)]
        headers: HashMap<String, String>,
    },
    /// SSE 数据块
    StreamChunk {
        id: String,
        data: String,
        #[serde(default, skip_serializing_if = "is_zero")]
        sequence: u32,
    },
    /// SSE 流式结束
    StreamEnd {
        id: String,
        #[serde(default)]
        error: Option<String>,
        #[serde(default, skip_serializing_if = "is_zero_u64")]
        duration_ms: u64,
        #[serde(default, skip_serializing_if = "is_zero")]
        total_chunks: u32,
    },

    /// TCP 连接建立（服务端 → 客户端）
    TcpConnect {
        conn_id: String,
    },
    /// TCP 数据传输（双向，base64）
    TcpData {
        conn_id: String,
        data: String,
        #[serde(default, skip_serializing_if = "is_zero")]
        sequence: u32,
    },
    /// TCP 连接关闭（双向）
    TcpClose {
        conn_id: String,
        #[serde(default)]
        error: Option<String>,
    },

    /// 心跳
    Ping {},
    Pong {},
}

impl Message {
    pub fn to_json(&self) -> String {
        serde_json::to_string(self).expect("Message serialize cannot fail")
    }

    pub fn type_name(&self) -> &'static str {
        match self {
            Message::Auth { .. } => "auth",
            Message::AuthOk { .. } => "auth_ok",
            Message::AuthError { .. } => "auth_error",
            Message::Request { .. } => "request",
            Message::Response { .. } => "response",
            Message::StreamStart { .. } => "stream_start",
            Message::StreamChunk { .. } => "stream_chunk",
            Message::StreamEnd { .. } => "stream_end",
            Message::TcpConnect { .. } => "tcp_connect",
            Message::TcpData { .. } => "tcp_data",
            Message::TcpClose { .. } => "tcp_close",
            Message::Ping { .. } => "ping",
            Message::Pong { .. } => "pong",
        }
    }

    pub fn parse(data: &str) -> Result<Message, serde_json::Error> {
        serde_json::from_str(data)
    }

    pub fn auth(token: &str, force: bool) -> Message {
        Message::Auth {
            token: token.to_string(),
            client_version: CLIENT_VERSION.to_string(),
            force,
        }
    }

    pub fn pong() -> Message {
        Message::Pong {}
    }

    #[allow(clippy::too_many_arguments)]
    pub fn response(
        id: &str,
        status: u16,
        headers: HashMap<String, String>,
        body: Option<String>,
        error: Option<String>,
        duration_ms: u64,
    ) -> Message {
        Message::Response {
            id: id.to_string(),
            status,
            headers,
            body,
            error,
            duration_ms,
        }
    }

    pub fn stream_start(id: &str, status: u16, headers: HashMap<String, String>) -> Message {
        Message::StreamStart {
            id: id.to_string(),
            status,
            headers,
        }
    }

    pub fn stream_chunk(id: &str, data: String, sequence: u32) -> Message {
        Message::StreamChunk {
            id: id.to_string(),
            data,
            sequence,
        }
    }

    pub fn stream_end(
        id: &str,
        error: Option<String>,
        duration_ms: u64,
        total_chunks: u32,
    ) -> Message {
        Message::StreamEnd {
            id: id.to_string(),
            error,
            duration_ms,
            total_chunks,
        }
    }

    pub fn tcp_data(conn_id: &str, data: &str, sequence: u32) -> Message {
        Message::TcpData {
            conn_id: conn_id.to_string(),
            data: data.to_string(),
            sequence,
        }
    }

    pub fn tcp_close(conn_id: &str, error: Option<String>) -> Message {
        Message::TcpClose {
            conn_id: conn_id.to_string(),
            error,
        }
    }
}

/// HTTP 转发时需要剥离的 hop-by-hop 头（与 TS 客户端一致）
pub const HOP_BY_HOP_HEADERS: &[&str] = &[
    "host",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-connection",
];

/// 从 targetUrl 解析 TCP 模式目标 (host, port)；解析失败回退 localhost:8080
pub fn parse_target(target_url: &str) -> (String, u16) {
    match url::Url::parse(target_url) {
        Ok(u) => {
            let host = u.host_str().unwrap_or("localhost").to_string();
            let port = u.port_or_known_default().unwrap_or(80);
            (host, port)
        }
        Err(_) => ("localhost".to_string(), 8080),
    }
}

/// 指数退避：base * 2^(factor-1)，封顶 5 分钟（与 TS 客户端一致）
pub fn backoff_delay_ms(base_ms: u64, factor: u32) -> u64 {
    let factor = factor.min(8);
    let raw = base_ms.saturating_mul(1u64 << factor.saturating_sub(1));
    raw.min(300_000)
}

/// 退避抖动：delay * (0.8..1.2)
pub fn jitter(delay_ms: u64) -> u64 {
    use rand::Rng;
    let f = rand::thread_rng().gen_range(0.8f64..1.2f64);
    ((delay_ms as f64) * f) as u64
}

/// 增量 UTF-8 解码（对齐 TextDecoder stream 语义 + WHATWG 替换语义的简化版）：
/// - 跨 chunk 的多字节序列缓存到下一轮；
/// - 非法字节替换为 U+FFFD 并从缓冲消费（F11：否则首个非法字节会让 decode 永远返回空）；
/// - 末尾不完整的多字节序列留给下一 chunk，流结束时用 flush() 以 U+FFFD 收尾。
pub struct Utf8StreamDecoder {
    buf: Vec<u8>,
}

impl Utf8StreamDecoder {
    pub fn new() -> Self {
        Self { buf: Vec::new() }
    }

    pub fn decode(&mut self, chunk: &[u8]) -> String {
        self.buf.extend_from_slice(chunk);
        let mut out = String::new();
        loop {
            match std::str::from_utf8(&self.buf) {
                Ok(s) => {
                    out.push_str(s);
                    self.buf.clear();
                    break;
                }
                Err(e) => {
                    let valid = e.valid_up_to();
                    if valid > 0 {
                        // SAFETY: valid_up_to 保证该前缀是合法 UTF-8
                        out.push_str(unsafe { std::str::from_utf8_unchecked(&self.buf[..valid]) });
                    }
                    self.buf.drain(..valid);
                    match e.error_len() {
                        // 确定的非法序列：替换为 U+FFFD 并消费 n 字节，绝不卡死
                        Some(n) => {
                            out.push('\u{FFFD}');
                            self.buf.drain(..n);
                        }
                        // 末尾不完整的多字节序列：缓存等待下一个 chunk
                        None => break,
                    }
                }
            }
        }
        out
    }

    /// 流结束：缓冲中残留的必然是不完整多字节序列，按 WHATWG 语义替换为 U+FFFD
    pub fn flush(&mut self) -> String {
        if self.buf.is_empty() {
            return String::new();
        }
        self.buf.clear();
        "\u{FFFD}".to_string()
    }
}

impl Default for Utf8StreamDecoder {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auth_serializes_like_ts_client() {
        let m = Message::auth("tok", true);
        let json = m.to_json();
        assert!(json.contains(r#""type":"auth""#), "{json}");
        assert!(json.contains(r#""force":true"#));
        assert!(json.contains(r#""token":"tok""#));
    }

    #[test]
    fn parses_python_server_messages() {
        let ok = Message::parse(
            r#"{"type":"auth_ok","domain":"dsh","tunnel_id":"t1","server_version":"0.1.0"}"#,
        )
        .unwrap();
        match ok {
            Message::AuthOk {
                domain, tunnel_id, ..
            } => {
                assert_eq!(domain, "dsh");
                assert_eq!(tunnel_id, "t1");
            }
            other => panic!("unexpected: {other:?}"),
        }

        let req = Message::parse(
            r#"{"type":"request","id":"r1","method":"GET","path":"/x","headers":{"a":"b"},"body":null,"timeout":30}"#,
        )
        .unwrap();
        match req {
            Message::Request { id, timeout, .. } => {
                assert_eq!(id, "r1");
                assert_eq!(timeout, Some(30.0));
            }
            other => panic!("unexpected: {other:?}"),
        }

        let ping = Message::parse(r#"{"type":"ping","timestamp":"now"}"#).unwrap();
        assert!(matches!(ping, Message::Ping { .. }));

        let tcp =
            Message::parse(r#"{"type":"tcp_connect","conn_id":"c1","timestamp":"now"}"#).unwrap();
        assert!(matches!(tcp, Message::TcpConnect { conn_id } if conn_id == "c1"));
    }

    #[test]
    fn roundtrip_all_types() {
        let msgs = vec![
            Message::pong(),
            Message::response("id", 200, HashMap::new(), Some("body".into()), None, 12),
            Message::stream_start("id", 200, HashMap::new()),
            Message::stream_chunk("id", "data".into(), 3),
            Message::stream_end("id", None, 100, 2),
            Message::tcp_data("c", "aGk=", 1),
            Message::tcp_close("c", Some("boom".into())),
        ];
        for m in msgs {
            let json = m.to_json();
            let back = Message::parse(&json).unwrap();
            let json2 = back.to_json();
            assert_eq!(json, json2, "roundtrip mismatch for {m:?}");
        }
    }

    #[test]
    fn parse_target_variants() {
        assert_eq!(
            parse_target("http://127.0.0.1:3080"),
            ("127.0.0.1".into(), 3080)
        );
        assert_eq!(parse_target("http://localhost"), ("localhost".into(), 80));
        assert_eq!(
            parse_target("https://example.com"),
            ("example.com".into(), 443)
        );
        assert_eq!(parse_target("not a url"), ("localhost".into(), 8080));
    }

    #[test]
    fn backoff_matches_ts_semantics() {
        // factor 封顶 8、delay 封顶 300s
        assert_eq!(backoff_delay_ms(5000, 1), 5000);
        assert_eq!(backoff_delay_ms(5000, 2), 10000);
        assert_eq!(backoff_delay_ms(5000, 3), 20000);
        assert_eq!(backoff_delay_ms(5000, 8), 300000);
        assert_eq!(backoff_delay_ms(5000, 100), 300000);
    }

    #[test]
    fn utf8_decoder_handles_split_multibyte() {
        let mut d = Utf8StreamDecoder::new();
        let full = "你好，隧道";
        let bytes = full.as_bytes();
        let (a, b) = bytes.split_at(4); // 劈开第二个“好”字
        let mut out = d.decode(a);
        out.push_str(&d.decode(b));
        out.push_str(&d.decode(bytes));
        assert_eq!(out, format!("{full}{full}"));
    }

    #[test]
    fn utf8_decoder_replaces_invalid_bytes_with_replacement_char() {
        // F11：非法字节替换为 U+FFFD 并推进缓冲，不卡死
        let mut d = Utf8StreamDecoder::new();
        let out = d.decode(&[b'a', 0xFF, b'b']);
        assert_eq!(out, "a\u{FFFD}b");
        // 非法字节之后的后续 chunk 仍能正常解码
        assert_eq!(d.decode(b"ok"), "ok");
    }

    #[test]
    fn utf8_decoder_never_stalls_on_leading_invalid_byte() {
        // 原 bug 场景：首个字节非法时 decode 从此永远返回空
        let mut d = Utf8StreamDecoder::new();
        assert_eq!(d.decode(&[0xFF]), "\u{FFFD}");
        assert_eq!(d.decode(&[0xFF]), "\u{FFFD}");
        assert_eq!(d.decode("你好".as_bytes()), "你好");
    }

    #[test]
    fn utf8_decoder_flush_emits_replacement_for_truncated_tail() {
        // 流结束时残留的截断序列以 U+FFFD 收尾，且不影响后续解码
        let mut d = Utf8StreamDecoder::new();
        let truncated = "你".as_bytes()[..1].to_vec(); // 0xE4，截断的 3 字节序列
        assert_eq!(d.decode(&truncated), "");
        assert_eq!(d.flush(), "\u{FFFD}");
        assert_eq!(d.decode(b"next"), "next");
        assert_eq!(d.flush(), "");
    }
}
