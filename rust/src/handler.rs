//! 进程内请求处理器（embedded handler）
//!
//! 不配 handler 时，隧道请求一律转发到 `config.target_url`——宿主必须自己监听
//! 一个本地端口。配了 handler 后，服务端下发的 `Message::Request` 先交给
//! handler，由它决定怎么答：
//!
//! - [`HandlerOutcome::Forward`]：不处理，回落 `target_url` 转发（语义与不配
//!   handler 时**逐字节一致**，含超时/503/504 错误映射）
//! - [`HandlerOutcome::Response`]：缓冲响应，一次性回 `Message::Response`
//! - [`HandlerOutcome::Stream`]：流式响应，按 SSE 语义回
//!   `stream_start` / `stream_chunk*` / `stream_end`
//!
//! 于是宿主进程可以「零本地端口」把自身能力（比如 agent 运行时里的一个
//! 处理函数）直接挂到公网隧道上，不必为了被转发而起一个 HTTP server。
//!
//! 作用域：handler 只作用于 HTTP 模式。TCP/UDP 隧道是裸字节流（服务端直接与
//! `target_url` 解析出的 host:port 建连），不经 handler。
//!
//! 线程语义：`handle` 在 tokio 工作线程上被 await；handler 内部若要做阻塞操作
//! （同步 IO、锁等待）应自行 `tokio::task::spawn_blocking`，否则会占住工作线程。

use std::collections::HashMap;

use futures_util::future::BoxFuture;
use futures_util::stream::BoxStream;

/// 递给 handler 的请求视图（字段已从 wire 协议解出）
#[derive(Debug, Clone)]
pub struct HandlerRequest {
    /// 请求 id：handler 不需要关心，回包时由客户端原样带上
    pub id: String,
    pub method: String,
    pub path: String,
    pub headers: HashMap<String, String>,
    pub body: Option<String>,
}

impl HandlerRequest {
    /// 大小写不敏感地取请求头
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// 缓冲响应
#[derive(Debug, Clone)]
pub struct HandlerResponse {
    pub status: u16,
    pub headers: HashMap<String, String>,
    pub body: Option<String>,
    /// 非空时按协议写入 `error` 字段（服务端据此判失败）
    pub error: Option<String>,
}

impl HandlerResponse {
    pub fn new(status: u16) -> Self {
        Self {
            status,
            headers: HashMap::new(),
            body: None,
            error: None,
        }
    }

    /// `text/plain; charset=utf-8` 文本响应
    pub fn text(status: u16, body: impl Into<String>) -> Self {
        Self::new(status).with_header("content-type", "text/plain; charset=utf-8").with_body(body)
    }

    /// `application/json; charset=utf-8` 响应（body 由调用方保证是合法 JSON）
    pub fn json(status: u16, body: impl Into<String>) -> Self {
        Self::new(status)
            .with_header("content-type", "application/json; charset=utf-8")
            .with_body(body)
    }

    /// 500 + 错误信息（写协议 `error` 字段，服务端按失败处理）
    pub fn error(message: impl Into<String>) -> Self {
        let message = message.into();
        let mut resp = Self::new(500);
        resp.error = Some(message.clone());
        resp.body = Some(message);
        resp
    }

    pub fn with_header(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.insert(key.into(), value.into());
        self
    }

    pub fn with_body(mut self, body: impl Into<String>) -> Self {
        self.body = Some(body.into());
        self
    }
}

/// 流式响应体：每个 `Ok(chunk)` 发一个 `stream_chunk`，`Err(msg)` 提前终止并写入
/// `stream_end.error`（与 SSE 读出错时的语义一致）
pub struct HandlerStream {
    pub status: u16,
    pub headers: HashMap<String, String>,
    pub chunks: BoxStream<'static, Result<String, String>>,
}

impl HandlerStream {
    pub fn new<S>(status: u16, headers: HashMap<String, String>, chunks: S) -> Self
    where
        S: futures_util::Stream<Item = Result<String, String>> + Send + 'static,
    {
        Self {
            status,
            headers,
            chunks: Box::pin(chunks),
        }
    }
}

impl std::fmt::Debug for HandlerStream {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HandlerStream")
            .field("status", &self.status)
            .field("headers", &self.headers)
            .finish_non_exhaustive()
    }
}

/// handler 的处理结果
#[derive(Debug)]
pub enum HandlerOutcome {
    /// 不处理 → 回落 `target_url` 转发
    Forward,
    /// 缓冲响应
    Response(HandlerResponse),
    /// 流式响应
    Stream(HandlerStream),
}

impl From<HandlerResponse> for HandlerOutcome {
    fn from(r: HandlerResponse) -> Self {
        HandlerOutcome::Response(r)
    }
}

/// 进程内请求处理器
///
/// 用 [`crate::client::TunnelClient::set_request_handler`]（或 builder
/// [`crate::client::TunnelClient::with_request_handler`]）装上；用
/// [`sync_handler`] 可以把同步闭包直接变成 handler。
pub trait RequestHandler: Send + Sync + 'static {
    fn handle(&self, request: HandlerRequest) -> BoxFuture<'static, HandlerOutcome>;
}

/// 把闭包适配成 [`RequestHandler`]（异步形态：闭包自己返回 future）
pub struct ClosureHandler<F>(F);

impl<F> ClosureHandler<F> {
    pub fn new(f: F) -> Self {
        Self(f)
    }
}

impl<F> RequestHandler for ClosureHandler<F>
where
    F: Fn(HandlerRequest) -> BoxFuture<'static, HandlerOutcome> + Send + Sync + 'static,
{
    fn handle(&self, request: HandlerRequest) -> BoxFuture<'static, HandlerOutcome> {
        (self.0)(request)
    }
}

/// 同步闭包 → handler 的便捷包装
///
/// ```no_run
/// use tunely::client::{TunnelClient, TunnelClientConfig};
/// use tunely::handler::{sync_handler, HandlerResponse};
///
/// let client = TunnelClient::new(TunnelClientConfig::default()).with_request_handler(
///     sync_handler(|req| {
///         if req.path == "/health" {
///             HandlerResponse::json(200, r#"{"ok":true}"#).into()
///         } else {
///             // 其余请求照旧转发到 target_url
///             tunely::handler::HandlerOutcome::Forward
///         }
///     }),
/// );
/// # let _ = client;
/// ```
pub fn sync_handler<F>(f: F) -> ClosureHandler<impl Fn(HandlerRequest) -> BoxFuture<'static, HandlerOutcome> + Send + Sync + 'static>
where
    F: Fn(&HandlerRequest) -> HandlerOutcome + Send + Sync + 'static,
{
    ClosureHandler::new(move |req: HandlerRequest| {
        let outcome = f(&req);
        Box::pin(std::future::ready(outcome)) as BoxFuture<'static, HandlerOutcome>
    })
}
