//! 隧道客户端：连接服务端，转发 HTTP 请求与 TCP 数据流到本地目标服务。
//!
//! 重连/退避/抢占语义与 typescript/src/client.ts 对齐：
//! 干净断开按固定间隔重连；错误按指数退避（封顶 5 分钟、factor 封顶 8、±20% 抖动）；
//! 认证被拒（already connected）累积 consecutive_reject 并在下一次认证自动带 force。

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use futures_util::{SinkExt, StreamExt};
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, Mutex, Notify};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use crate::protocol::{backoff_delay_ms, jitter, parse_target, Message, HOP_BY_HOP_HEADERS};

fn info(msg: impl AsRef<str>) {
    println!("{}", msg.as_ref());
}
fn warn(msg: impl AsRef<str>) {
    eprintln!("{}", msg.as_ref());
}
fn error(msg: impl AsRef<str>) {
    eprintln!("{}", msg.as_ref());
}

/// 客户端配置
#[derive(Debug, Clone)]
pub struct TunnelClientConfig {
    /// 服务端 WebSocket URL（ws:// 或 wss://）
    pub server_url: String,
    /// 隧道令牌
    pub token: String,
    /// 本地目标服务 URL（HTTP 模式作 base；TCP 模式解析出 host:port）
    pub target_url: String,
    /// 重连基础间隔，默认 5s
    pub reconnect_interval: Duration,
    /// 最大重连次数（0 = 无限）
    pub max_reconnect_attempts: u32,
    /// 请求默认超时（请求消息未带 timeout 时使用），默认 300s
    pub request_timeout: Duration,
    /// 是否总是强制抢占已有连接
    pub force: bool,
}

impl Default for TunnelClientConfig {
    fn default() -> Self {
        Self {
            server_url: "ws://localhost:8000/ws/tunnel".to_string(),
            token: String::new(),
            target_url: "http://localhost:8080".to_string(),
            reconnect_interval: Duration::from_secs(5),
            max_reconnect_attempts: 0,
            request_timeout: Duration::from_secs(300),
            force: false,
        }
    }
}

/// 事件回调类型
pub type StrCallback = Box<dyn Fn(&str) + Send + Sync>;
pub type UnitCallback = Box<dyn Fn() + Send + Sync>;
pub type RequestCallback = Box<dyn Fn(&str, &str, &str) + Send + Sync>;

/// 事件回调集合（均为可选）
#[derive(Default)]
pub struct Events {
    pub on_connect: Option<StrCallback>,
    pub on_disconnect: Option<UnitCallback>,
    pub on_error: Option<StrCallback>,
    /// (request_id, method, path)
    pub on_request: Option<RequestCallback>,
}

/// 单次连接内的失败原因
#[derive(Debug)]
pub enum ConnectError {
    /// 服务端 auth_error（含拒绝信息）
    Auth(String),
    /// 传输层错误
    Io(String),
}

impl std::fmt::Display for ConnectError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConnectError::Auth(m) => write!(f, "认证失败: {m}"),
            ConnectError::Io(m) => write!(f, "{m}"),
        }
    }
}

/// 运行期计数（跨重连保留）
#[derive(Default)]
struct RunState {
    connected: AtomicBool,
    was_connected: AtomicBool,
    reconnect_count: AtomicU32,
    consecutive_reject: AtomicU32,
}

/// 本地 TCP 连接状态（TCP 模式）
struct TcpConn {
    write: Mutex<OwnedWriteHalf>,
    close_sent: AtomicBool,
}

/// 单次 WebSocket 会话内共享的发送通道与本地连接表
#[derive(Clone)]
struct Session {
    tx: mpsc::UnboundedSender<Message>,
    tcp: Arc<Mutex<HashMap<String, Arc<TcpConn>>>>,
    http: reqwest::Client,
    target_base: String,
    target_host: String,
    target_port: u16,
    request_timeout: Duration,
}

/// 隧道客户端
pub struct TunnelClient {
    config: TunnelClientConfig,
    events: Mutex<Events>,
    running: AtomicBool,
    stop: Arc<Notify>,
}

impl TunnelClient {
    pub fn new(config: TunnelClientConfig) -> Self {
        Self {
            config,
            events: Mutex::new(Events::default()),
            running: AtomicBool::new(false),
            stop: Arc::new(Notify::new()),
        }
    }

    pub fn on_connect<F: Fn(&str) + Send + Sync + 'static>(&self, f: F) {
        if let Ok(mut e) = self.events.try_lock() {
            e.on_connect = Some(Box::new(f));
        }
    }

    pub fn on_disconnect<F: Fn() + Send + Sync + 'static>(&self, f: F) {
        if let Ok(mut e) = self.events.try_lock() {
            e.on_disconnect = Some(Box::new(f));
        }
    }

    pub fn on_error<F: Fn(&str) + Send + Sync + 'static>(&self, f: F) {
        if let Ok(mut e) = self.events.try_lock() {
            e.on_error = Some(Box::new(f));
        }
    }

    pub fn on_request<F: Fn(&str, &str, &str) + Send + Sync + 'static>(&self, f: F) {
        if let Ok(mut e) = self.events.try_lock() {
            e.on_request = Some(Box::new(f));
        }
    }

    fn fire_disconnect(&self) {
        if let Ok(e) = self.events.try_lock() {
            if let Some(f) = &e.on_disconnect {
                f();
            }
        }
    }

    fn fire_error(&self, msg: &str) {
        if let Ok(e) = self.events.try_lock() {
            if let Some(f) = &e.on_error {
                f(msg);
            }
        }
    }

    /// 运行客户端：自动重连直到 stop()。
    pub async fn run(&self) {
        self.running.store(true, Ordering::SeqCst);
        let state = Arc::new(RunState::default());
        let (target_host, target_port) = parse_target(&self.config.target_url);
        let target_base = self.config.target_url.trim_end_matches('/').to_string();

        while self.running.load(Ordering::SeqCst) {
            let force = self.config.force
                || (state.was_connected.load(Ordering::SeqCst)
                    && state.consecutive_reject.load(Ordering::SeqCst) > 0);

            match self
                .connect_and_run(
                    &state,
                    force,
                    target_base.clone(),
                    target_host.clone(),
                    target_port,
                )
                .await
            {
                Ok(()) => {
                    if state.connected.swap(false, Ordering::SeqCst) {
                        self.fire_disconnect();
                    }
                    if !self.running.load(Ordering::SeqCst) {
                        break;
                    }
                    info(format!(
                        "连接已关闭，{:.1}秒后重连",
                        self.config.reconnect_interval.as_secs_f32()
                    ));
                    self.sleep_interruptible(self.config.reconnect_interval)
                        .await;
                }
                Err(err) => {
                    if !self.running.load(Ordering::SeqCst) {
                        break;
                    }
                    if state.connected.swap(false, Ordering::SeqCst) {
                        self.fire_disconnect();
                    }
                    self.fire_error(&err.to_string());

                    let reconnect_count = state.reconnect_count.fetch_add(1, Ordering::SeqCst) + 1;
                    let max = self.config.max_reconnect_attempts;
                    if max > 0 && reconnect_count > max {
                        error(format!("超过最大重连次数 ({max})，停止"));
                        break;
                    }

                    let msg = err.to_string();
                    let is_reject =
                        msg.contains("already connected") || msg.contains("已有活跃连接");
                    if is_reject {
                        state.consecutive_reject.fetch_add(1, Ordering::SeqCst);
                    } else {
                        state.consecutive_reject.store(0, Ordering::SeqCst);
                    }

                    let backoff_factor =
                        (reconnect_count + state.consecutive_reject.load(Ordering::SeqCst)).min(8);
                    let delay = jitter(backoff_delay_ms(
                        self.config.reconnect_interval.as_millis() as u64,
                        backoff_factor,
                    ));
                    warn(format!(
                        "连接断开: {err}，{:.1}秒后重连 (第 {reconnect_count} 次, backoff={backoff_factor})",
                        delay as f32 / 1000.0
                    ));
                    self.sleep_interruptible(Duration::from_millis(delay)).await;
                }
            }
        }
    }

    /// 停止客户端（幂等；当前连接与重连等待都会尽快退出）
    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
        self.stop.notify_waiters();
    }

    async fn sleep_interruptible(&self, d: Duration) {
        let _ = tokio::time::timeout(d, self.stop.notified()).await;
    }

    async fn connect_and_run(
        &self,
        state: &Arc<RunState>,
        force: bool,
        target_base: String,
        target_host: String,
        target_port: u16,
    ) -> Result<(), ConnectError> {
        info(format!("正在连接到 {}...", self.config.server_url));

        let (ws, _resp) = connect_async(&self.config.server_url)
            .await
            .map_err(|e| ConnectError::Io(format!("WebSocket 连接失败: {e}")))?;

        let (mut sink, mut stream) = ws.split();
        let (tx, mut rx) = mpsc::unbounded_channel::<Message>();
        let writer = tokio::spawn(async move {
            while let Some(msg) = rx.recv().await {
                if sink.send(WsMessage::Text(msg.to_json())).await.is_err() {
                    break;
                }
            }
            let _ = sink.close().await;
        });

        let session = Session {
            tx: tx.clone(),
            tcp: Arc::new(Mutex::new(HashMap::new())),
            http: reqwest::Client::builder()
                .build()
                .map_err(|e| ConnectError::Io(format!("http client: {e}")))?,
            target_base,
            target_host,
            target_port,
            request_timeout: self.config.request_timeout,
        };

        // 发送认证
        tx.send(Message::auth(&self.config.token, force))
            .map_err(|_| ConnectError::Io("发送认证失败".into()))?;

        let failure: Arc<Mutex<Option<ConnectError>>> = Arc::new(Mutex::new(None));

        loop {
            let next = tokio::select! {
                biased;
                _ = self.stop.notified() => None,
                n = stream.next() => n,
            };

            let ws_msg = match next {
                Some(Ok(m)) => m,
                Some(Err(e)) => {
                    error(format!("WebSocket 错误: {e}"));
                    *failure.lock().await = Some(ConnectError::Io(e.to_string()));
                    break;
                }
                None => break, // 服务端关闭 → 干净断开
            };

            let text = match ws_msg {
                WsMessage::Text(t) => t,
                WsMessage::Close(_) => break,
                _ => continue,
            };

            let msg = match Message::parse(&text) {
                Ok(m) => m,
                Err(e) => {
                    warn(format!("JSON 解析错误: {e}"));
                    continue;
                }
            };

            match msg {
                Message::AuthOk { domain, .. } => {
                    state.connected.store(true, Ordering::SeqCst);
                    state.was_connected.store(true, Ordering::SeqCst);
                    state.reconnect_count.store(0, Ordering::SeqCst);
                    state.consecutive_reject.store(0, Ordering::SeqCst);
                    info(format!("已连接: domain={domain}"));
                    if let Ok(e) = self.events.try_lock() {
                        if let Some(f) = &e.on_connect {
                            f(&domain);
                        }
                    }
                }
                Message::AuthError { error: reason, .. } => {
                    error(format!("认证失败: {reason}"));
                    *failure.lock().await = Some(ConnectError::Auth(reason));
                    break;
                }
                Message::Ping {} => {
                    let _ = session.tx.send(Message::pong());
                }
                Message::Request {
                    id,
                    method,
                    path,
                    headers,
                    body,
                    timeout,
                } => {
                    if let Ok(e) = self.events.try_lock() {
                        if let Some(f) = &e.on_request {
                            f(&id, &method, &path);
                        }
                    }
                    spawn_http_request(&session, id, method, path, headers, body, timeout);
                }
                Message::TcpConnect { conn_id } => handle_tcp_connect(&session, &conn_id).await,
                Message::TcpData { conn_id, data, .. } => {
                    handle_tcp_data(&session, &conn_id, &data).await
                }
                Message::TcpClose { conn_id, .. } => {
                    handle_server_tcp_close(&session, &conn_id).await
                }
                other => {
                    warn(format!("未知消息类型: {}", other.type_name()));
                }
            }

            if !self.running.load(Ordering::SeqCst) {
                break;
            }
        }

        // 丢弃全部本地 TCP 连接（服务端会重新分配 conn_id），防止 socket 泄漏。
        // 不等待 writer：残留的 TCP 读任务还持有 tx 克隆，若目标迟迟不关写侧会拖住整个重连；
        // WebSocket 已死，直接中止 writer，读任务会在后续 send 失败时自行退出。
        cleanup_tcp(&session).await;
        drop(tx);
        writer.abort();

        let outcome = failure.lock().await.take();
        match outcome {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }
}

// ============== HTTP 模式 ==============

fn spawn_http_request(
    session: &Session,
    id: String,
    method: String,
    path: String,
    headers: HashMap<String, String>,
    body: Option<String>,
    timeout: Option<f64>,
) {
    let session = session.clone();
    tokio::spawn(async move {
        let start = std::time::Instant::now();
        let url = format!("{}{}", session.target_base, path);
        let http_method =
            reqwest::Method::from_bytes(method.as_bytes()).unwrap_or(reqwest::Method::GET);

        let timeout_secs = timeout.unwrap_or(session.request_timeout.as_secs_f64());
        let timeout_dur = Duration::from_secs_f64(timeout_secs.max(0.0));

        let mut req = session.http.request(http_method, &url).timeout(timeout_dur);
        for (k, v) in &headers {
            if HOP_BY_HOP_HEADERS.contains(&k.to_lowercase().as_str()) {
                continue;
            }
            if let (Ok(name), Ok(val)) = (
                reqwest::header::HeaderName::from_bytes(k.as_bytes()),
                reqwest::header::HeaderValue::from_str(v),
            ) {
                req = req.header(name, val);
            }
        }
        if let Some(b) = body {
            req = req.body(b);
        }

        match req.send().await {
            Ok(resp) => {
                let status = resp.status().as_u16();
                let mut resp_headers: HashMap<String, String> = HashMap::new();
                for (name, value) in resp.headers().iter() {
                    let key = name.as_str().to_string();
                    let val = String::from_utf8_lossy(value.as_bytes()).to_string();
                    resp_headers
                        .entry(key)
                        .and_modify(|existing| {
                            existing.push_str(", ");
                            existing.push_str(&val);
                        })
                        .or_insert(val);
                }

                let is_sse = resp_headers
                    .get("content-type")
                    .map(|c| c.to_lowercase().contains("text/event-stream"))
                    .unwrap_or(false);

                if is_sse {
                    handle_sse(&session, &id, status, resp_headers, resp, start).await;
                } else {
                    let body = resp.text().await.unwrap_or_default();
                    let duration = start.elapsed().as_millis() as u64;
                    let _ = session.tx.send(Message::response(
                        &id,
                        status,
                        resp_headers,
                        Some(body),
                        None,
                        duration,
                    ));
                }
            }
            Err(e) => {
                let duration = start.elapsed().as_millis() as u64;
                let (status, error) = if e.is_timeout() {
                    (504, "Target service timeout".to_string())
                } else if e.is_connect() {
                    (503, format!("Target service unavailable: {e}"))
                } else {
                    (500, format!("{e}"))
                };
                let _ = session.tx.send(Message::response(
                    &id,
                    status,
                    HashMap::new(),
                    None,
                    Some(error),
                    duration,
                ));
            }
        }
    });
}

async fn handle_sse(
    session: &Session,
    id: &str,
    status: u16,
    headers: HashMap<String, String>,
    mut resp: reqwest::Response,
    start: std::time::Instant,
) {
    let _ = session.tx.send(Message::stream_start(id, status, headers));

    let mut decoder = crate::protocol::Utf8StreamDecoder::new();
    let mut seq: u32 = 0;
    let mut error_msg: Option<String> = None;

    loop {
        match resp.chunk().await {
            Ok(Some(chunk)) => {
                let text = decoder.decode(&chunk);
                if !text.is_empty() {
                    let _ = session.tx.send(Message::stream_chunk(id, text, seq));
                    seq += 1;
                }
            }
            Ok(None) => break,
            Err(e) => {
                error(format!("SSE 流读取错误: {e}"));
                error_msg = Some(e.to_string());
                break;
            }
        }
    }

    // 冲洗缓存的未完成多字节序列
    let tail = decoder.decode(&[]);
    if !tail.is_empty() {
        let _ = session.tx.send(Message::stream_chunk(id, tail, seq));
        seq += 1;
    }

    let duration = start.elapsed().as_millis() as u64;
    let _ = session
        .tx
        .send(Message::stream_end(id, error_msg, duration, seq));
}

// ============== TCP 模式 ==============

async fn handle_tcp_connect(session: &Session, conn_id: &str) {
    {
        let tcp = session.tcp.lock().await;
        if tcp.contains_key(conn_id) {
            warn(format!("TCP 连接已存在，忽略重复的 tcp_connect: {conn_id}"));
            return;
        }
    }

    match TcpStream::connect((session.target_host.as_str(), session.target_port)).await {
        Ok(socket) => {
            let (read, write) = socket.into_split();
            let conn = Arc::new(TcpConn {
                write: Mutex::new(write),
                close_sent: AtomicBool::new(false),
            });
            session
                .tcp
                .lock()
                .await
                .insert(conn_id.to_string(), conn.clone());

            let session = session.clone();
            let conn_id = conn_id.to_string();
            tokio::spawn(async move {
                tcp_read_loop(&session, &conn_id, conn, read).await;
            });
        }
        Err(e) => {
            // 连接失败：回执带 error 的 tcp_close，服务端据此关闭外部连接
            error(format!(
                "TCP 连接错误: {conn_id} -> {}:{}, {e}",
                session.target_host, session.target_port
            ));
            let _ = session
                .tx
                .send(Message::tcp_close(conn_id, Some(e.to_string())));
        }
    }
}

async fn tcp_read_loop(
    session: &Session,
    conn_id: &str,
    conn: Arc<TcpConn>,
    mut read: OwnedReadHalf,
) {
    use tokio::io::AsyncReadExt;
    let mut buf = vec![0u8; 65536];
    let mut seq: u32 = 0;
    loop {
        match read.read(&mut buf).await {
            Ok(0) => {
                finish_tcp(session, conn_id, &conn, None).await;
                break;
            }
            Ok(n) => {
                let encoded = base64::engine::general_purpose::STANDARD.encode(&buf[..n]);
                if session
                    .tx
                    .send(Message::tcp_data(conn_id, &encoded, seq))
                    .is_err()
                {
                    break; // WebSocket 已断
                }
                seq += 1;
            }
            Err(e) => {
                finish_tcp(session, conn_id, &conn, Some(e.to_string())).await;
                break;
            }
        }
    }
}

/// 本地侧结束（EOF/error）：恰好回执一个 tcp_close（幂等）并清理映射
async fn finish_tcp(session: &Session, conn_id: &str, conn: &Arc<TcpConn>, error: Option<String>) {
    use tokio::io::AsyncWriteExt;
    if !conn.close_sent.swap(true, Ordering::SeqCst) {
        let _ = session.tx.send(Message::tcp_close(conn_id, error));
    }
    {
        let mut w = conn.write.lock().await;
        let _ = w.shutdown().await;
    }
    session.tcp.lock().await.remove(conn_id);
}

async fn handle_tcp_data(session: &Session, conn_id: &str, data: &str) {
    let conn = session.tcp.lock().await.get(conn_id).cloned();
    let Some(conn) = conn else {
        warn(format!("收到未知 TCP 连接的数据: {conn_id}"));
        return;
    };
    let bytes = match base64::engine::general_purpose::STANDARD.decode(data) {
        Ok(b) => b,
        Err(e) => {
            error(format!("TCP 数据 base64 解码错误: {conn_id}, {e}"));
            return;
        }
    };
    use tokio::io::AsyncWriteExt;
    let mut w = conn.write.lock().await;
    let _ = w.write_all(&bytes).await; // 写失败会随后以 read 侧 EOF/error 收敛
}

async fn handle_server_tcp_close(session: &Session, conn_id: &str) {
    let conn = session.tcp.lock().await.remove(conn_id);
    let Some(conn) = conn else {
        warn(format!("尝试关闭未知 TCP 连接: {conn_id}"));
        return;
    };
    // 服务端主动关闭：不回执 tcp_close
    conn.close_sent.store(true, Ordering::SeqCst);
    use tokio::io::AsyncWriteExt;
    {
        let mut w = conn.write.lock().await;
        let _ = w.shutdown().await;
    }
}

async fn cleanup_tcp(session: &Session) {
    use tokio::io::AsyncWriteExt;
    let mut tcp = session.tcp.lock().await;
    for conn in tcp.values() {
        conn.close_sent.store(true, Ordering::SeqCst);
        if let Ok(mut w) = conn.write.try_lock() {
            let _ = w.shutdown().await;
        }
    }
    tcp.clear();
}
