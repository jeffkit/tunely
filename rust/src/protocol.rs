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

fn default_client_version() -> String {
    CLIENT_VERSION.to_string()
}

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
        /// 反序列化缺省时回退 CLIENT_VERSION（与 TS/py 的可选语义对称；
        /// 发送侧恒填 CLIENT_VERSION 真实版本）
        #[serde(default = "default_client_version")]
        client_version: String,
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        force: bool,
        /// 协议 v2 能力协商：客户端支持的能力。缺字段 = 空 vec（不声明任何能力）。
        /// 铁律：客户端只许声明自己已实现的能力；发送侧恒为空 vec 且
        /// skip_serializing_if 保证空 vec 不上线（wire 最小变化）。
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        capabilities: Vec<String>,
    },
    /// 认证成功
    AuthOk {
        domain: String,
        tunnel_id: String,
        #[serde(default)]
        server_version: Option<String>,
        /// 协议 v2 能力协商：协商启用的能力（服务端注册表 ∩ 客户端声明）。
        /// 缺字段 = 空 vec（旧服务端 auth_ok 不带该字段，安全）。
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        capabilities: Vec<String>,
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
        /// 协议 v2 chunked_http（T3）：服务端放行非 SSE 大响应流式回传。
        /// rust 客户端不实现该能力、也不在 auth 声明（PROTOCOL.md 登记表
        /// 已注明）；反序列化按缺省 false 容忍新 wire。
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        stream_ok: bool,
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
        /// 协议 v2 chunked_http（T3）：data 编码（"plain"=UTF-8 文本 /
        /// "base64"=二进制字节，+33% 局限见 PROTOCOL_V2 §4）。rust 客户端
        /// 仅发 SSE 文本块（恒 plain），缺省 None 序列化时省略。
        #[serde(default, skip_serializing_if = "Option::is_none")]
        encoding: Option<String>,
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
            // T2 起客户端声明已实现的能力（铁律：只许声明已实现的）；
            // 修改本 vec 前先确认对应能力已在本客户端实现
            capabilities: vec!["binary_frames".to_string()],
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
            // rust 仅发 SSE 文本块（plain），chunked_http 的 base64 块不适用
            encoding: None,
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

// ============== binary_frames 帧编解码（协议 v2 T2，docs/PROTOCOL_V2.md §1） ==============
//
// WS binary 帧（仅 tcp_data 一个类型）：
//   [0x02]     1B  协议版本标记（v2）
//   [0x01]     1B  帧类型：0x01 = tcp_data
//   [16B]      conn_id，UUID v4 原始字节（JSON 控制面仍是 36 字符串形式）
//   [payload]  原始字节（无 base64、无 JSON、无 sequence——WS 有序，接收侧不依赖）
//
// tcp_close 保持 JSON（低频、携带 error）。解码遇版本/类型/长度不对返回 Err，
// 调用方按畸形帧丢弃（F10 语义）。UUID 转换手写 parse/format（不引 uuid crate）。

/// 协议版本标记（v2）
pub const FRAME_PROTOCOL_VERSION: u8 = 0x02;
/// 帧类型：tcp_data
pub const FRAME_TYPE_TCP_DATA: u8 = 0x01;
/// 帧头长度：1B version + 1B type + 16B conn_id
pub const FRAME_HEADER_LEN: usize = 18;

/// UUID 36 字符串 → 16 原始字节。
///
/// 仅做格式校验（长度 + 连字符位置 + hex 字符），不校验版本/变体位——
/// 与 python `uuid.UUID(...).bytes` 语义一致（任意 16 字节可往返）。
pub fn uuid_to_bytes(uuid_str: &str) -> Result<[u8; 16], String> {
    let invalid = || format!("invalid uuid: {uuid_str}");
    let b = uuid_str.as_bytes();
    if b.len() != 36 || b[8] != b'-' || b[13] != b'-' || b[18] != b'-' || b[23] != b'-' {
        return Err(invalid());
    }
    let mut out = [0u8; 16];
    let mut oi = 0usize;
    let mut pending: Option<u8> = None;
    for (i, &c) in b.iter().enumerate() {
        if matches!(i, 8 | 13 | 18 | 23) {
            continue;
        }
        let v = (c as char).to_digit(16).ok_or_else(invalid)? as u8;
        match pending.take() {
            None => pending = Some(v),
            Some(hi) => {
                out[oi] = (hi << 4) | v;
                oi += 1;
            }
        }
    }
    debug_assert_eq!(oi, 16);
    Ok(out)
}

/// 16 原始字节 → 小写 36 字符 UUID 字符串。
pub fn bytes_to_uuid(bytes: &[u8; 16]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(36);
    for (i, &byte) in bytes.iter().enumerate() {
        if matches!(i, 4 | 6 | 8 | 10) {
            s.push('-');
        }
        s.push(HEX[(byte >> 4) as usize] as char);
        s.push(HEX[(byte & 0x0f) as usize] as char);
    }
    s
}

/// tcp_data 二进制帧编码：0x02 0x01 + UUID 原始字节 + 原始 payload。
/// conn_id 非 UUID 字符串返回 Err。
pub fn encode_tcp_data_frame(conn_id: &str, payload: &[u8]) -> Result<Vec<u8>, String> {
    let mut frame = Vec::with_capacity(FRAME_HEADER_LEN + payload.len());
    frame.push(FRAME_PROTOCOL_VERSION);
    frame.push(FRAME_TYPE_TCP_DATA);
    frame.extend_from_slice(&uuid_to_bytes(conn_id)?);
    frame.extend_from_slice(payload);
    Ok(frame)
}

/// tcp_data 二进制帧解码 → (conn_id 36 字符串, payload 原始字节)。
///
/// 版本/类型/长度不对返回 Err（调用方按畸形帧丢弃，F10 语义）；
/// conn_id 16 字节不校验版本位（与 python uuid 语义一致）。
pub fn decode_tcp_data_frame(frame: &[u8]) -> Result<(String, Vec<u8>), String> {
    if frame.len() < FRAME_HEADER_LEN {
        return Err(format!("invalid frame: too short ({})", frame.len()));
    }
    if frame[0] != FRAME_PROTOCOL_VERSION {
        return Err(format!(
            "invalid frame: unsupported version 0x{:02x}",
            frame[0]
        ));
    }
    if frame[1] != FRAME_TYPE_TCP_DATA {
        return Err(format!("invalid frame: unsupported type 0x{:02x}", frame[1]));
    }
    let mut id = [0u8; 16];
    id.copy_from_slice(&frame[2..FRAME_HEADER_LEN]);
    Ok((bytes_to_uuid(&id), frame[FRAME_HEADER_LEN..].to_vec()))
}

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

    #[test]
    fn auth_declares_binary_frames_capability() {
        // T2：客户端声明已实现的能力（auth wire 含 capabilities）
        match Message::auth("tok", false) {
            Message::Auth { capabilities, .. } => {
                assert_eq!(capabilities, vec!["binary_frames".to_string()])
            }
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn frame_roundtrip() {
        let conn_id = "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b";
        let payload = b"hello tcp \x00\x01\xff binary";
        let frame = encode_tcp_data_frame(conn_id, payload).unwrap();
        let (out_id, out_payload) = decode_tcp_data_frame(&frame).unwrap();
        assert_eq!(out_id, conn_id);
        assert_eq!(out_payload, payload.to_vec());
    }

    #[test]
    fn frame_layout() {
        let conn_id = "00112233-4455-4677-8899-aabbccddeeff";
        let frame = encode_tcp_data_frame(conn_id, b"abc").unwrap();
        assert_eq!(frame.len(), FRAME_HEADER_LEN + 3);
        assert_eq!(frame[0], 0x02);
        assert_eq!(frame[1], 0x01);
        assert_eq!(&frame[2..18], &b"\x00\x11\x22\x33\x44\x55\x46\x77\x88\x99\xaa\xbb\xcc\xdd\xee\xff"[..]);
        assert_eq!(&frame[18..], b"abc");
    }

    #[test]
    fn frame_empty_payload() {
        let conn_id = "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b";
        let frame = encode_tcp_data_frame(conn_id, b"").unwrap();
        assert_eq!(frame.len(), FRAME_HEADER_LEN);
        let (out_id, payload) = decode_tcp_data_frame(&frame).unwrap();
        assert_eq!(out_id, conn_id);
        assert!(payload.is_empty());
    }

    #[test]
    fn frame_decode_rejects_malformed() {
        // 过短
        assert!(decode_tcp_data_frame(&[]).is_err());
        assert!(decode_tcp_data_frame(&[0x02, 0x01, 0x00]).is_err());
        assert!(decode_tcp_data_frame(&[0x02; 17]).is_err());
        // 错版本
        let mut bad = encode_tcp_data_frame("3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b", b"x").unwrap();
        bad[0] = 0x01;
        assert!(decode_tcp_data_frame(&bad)
            .unwrap_err()
            .contains("version"));
        // 错类型（仅 tcp_data=0x01 一个类型）
        let mut bad = encode_tcp_data_frame("3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b", b"x").unwrap();
        bad[1] = 0x09;
        assert!(decode_tcp_data_frame(&bad).unwrap_err().contains("type"));
    }

    #[test]
    fn frame_encode_rejects_invalid_conn_id() {
        assert!(encode_tcp_data_frame("not-a-uuid", b"x").is_err());
        assert!(encode_tcp_data_frame("3f2a1b4c5d6e4f809a1b2c3d4e5f6a7b", b"x").is_err());
    }

    #[test]
    fn uuid_conversion_matches_python_semantics() {
        // 已知向量（与 python uuid.UUID(...).bytes 一致）
        let bytes = uuid_to_bytes("00112233-4455-4677-8899-AABBCCDDEEFF").unwrap();
        assert_eq!(
            bytes,
            [
                0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x46, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc,
                0xdd, 0xee, 0xff
            ]
        );
        // bytes_to_uuid 输出小写规范格式
        assert_eq!(
            bytes_to_uuid(&bytes),
            "00112233-4455-4677-8899-aabbccddeeff"
        );
        // 畸形输入拒绝
        assert!(uuid_to_bytes("not-a-uuid").is_err());
        assert!(uuid_to_bytes("3f2a1b4c5d6e4f809a1b2c3d4e5f6a7b").is_err());
        assert!(uuid_to_bytes("3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7g").is_err());
        // 任意 16 字节可往返（不校验版本位）
        let raw = [0xffu8; 16];
        assert_eq!(uuid_to_bytes(&bytes_to_uuid(&raw)).unwrap(), raw);
    }
}
