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
use tokio::net::{TcpStream, UdpSocket};
use tokio::sync::{mpsc, Mutex, Notify};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message as WsMessage;

use crate::protocol::{
    backoff_delay_ms, decode_tcp_data_frame, decode_udp_data_frame, encode_tcp_data_frame,
    encode_udp_data_frame, jitter, parse_target, Message, FRAME_TYPE_UDP_DATA,
    HOP_BY_HOP_HEADERS,
};

fn info(msg: impl AsRef<str>) {
    println!("{}", msg.as_ref());
}
fn warn(msg: impl AsRef<str>) {
    eprintln!("{}", msg.as_ref());
}
fn error(msg: impl AsRef<str>) {
    eprintln!("{}", msg.as_ref());
}
/// 竞态噪音专用：并发关闭窗口里服务端迟到帧命中的连接属正常时序，
/// 默认静默，设 TUNELY_DEBUG=1 才输出（防 churn 期刷屏）
fn debug(msg: impl AsRef<str>) {
    if std::env::var_os("TUNELY_DEBUG").is_some() {
        eprintln!("{}", msg.as_ref());
    }
}

/// 归一化服务端下发的请求路径：确保以 "/" 开头。
///
/// 防止 "@evil/" 这类不以 "/" 开头的 path 在 URL 拼接时改写 authority
/// （如 `http://127.0.0.1:3080` + `@evil/` → 请求打到 evil 主机，SSRF）。
pub fn normalize_path(path: &str) -> String {
    if path.starts_with('/') {
        path.to_string()
    } else {
        format!("/{}", path)
    }
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
    /// keepalive：周期性发送协议 ping，默认 25s
    pub keepalive_interval: Duration,
    /// keepalive：超过该时长未收到 pong 判定连接死亡并重连，默认 45s
    pub keepalive_timeout: Duration,
    /// 多隧道模式下的会话标签（单隧道为 None）；用于连接期日志前缀，
    /// 认证成功后的日志自带 domain 不依赖此值
    pub name: Option<String>,
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
            keepalive_interval: Duration::from_secs(25),
            keepalive_timeout: Duration::from_secs(45),
            name: None,
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

/// 本地 UDP 会话状态（协议 v2 udp）
///
/// socket 为连接型（connect 到目标），读侧由 recv_task 独占轮询；
/// 外部关闭 = 从会话表移除 + abort 读任务（drop 掉所有 Arc 即关闭 socket）。
struct UdpConn {
    socket: Arc<UdpSocket>,
    recv_task: tokio::task::JoinHandle<()>,
}

/// WS 发送通道容量：有界队列提供背压——本地产生数据快于 WS 出口时，
/// 生产者（HTTP/SSE/TCP 读循环）在此阻塞而不是无界占用内存。
const TX_CHANNEL_CAPACITY: usize = 256;

/// WS 出站帧：控制面 JSON 消息 or 数据面二进制帧（协议 v2 binary_frames）
enum OutFrame {
    Text(String),
    Binary(Vec<u8>),
}

impl From<Message> for OutFrame {
    fn from(m: Message) -> Self {
        OutFrame::Text(m.to_json())
    }
}

/// 单次 WebSocket 会话内共享的发送通道与本地连接表
#[derive(Clone)]
struct Session {
    tx: mpsc::Sender<OutFrame>,
    tcp: Arc<Mutex<HashMap<String, Arc<TcpConn>>>>,
    /// session_id → 本地 UDP 会话（协议 v2 udp；每会话一个到目标的 socket）
    udp: Arc<Mutex<HashMap<String, Arc<UdpConn>>>>,
    http: reqwest::Client,
    target_base: String,
    target_host: String,
    target_port: u16,
    request_timeout: Duration,
    /// 本连接是否协商了 binary_frames 能力（协议 v2，AuthOk.capabilities 快照；
    /// 未协商 = false，tcp_data 全走 JSON+base64，行为与 0.7.x 一致）
    binary_frames: bool,
    /// 本连接是否协商了 udp 能力（协议 v2 T4；未协商 = false，收到 udp_open /
    /// 0x03 帧一律丢弃，行为与 0.8.0 一致）
    udp_enabled: bool,
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

    /// 日志前缀：多隧道模式下区分会话；单隧道为空串（日志与 0.3.x 逐字节一致）
    fn log_prefix(&self) -> String {
        self.config
            .name
            .as_deref()
            .map(|n| format!("[{n}] "))
            .unwrap_or_default()
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
        // parse_target 对无 scheme 输入（如 "127.0.0.1:8902" 会被 URL 解析当 scheme）
        // 静默回退 localhost:8080——历史上吃过亏，这里把解析结果亮出来
        if session_target_looks_suspicious(&self.config.target_url, &target_host, target_port) {
            warn(format!(
                "{}target 解析可疑: config='{}' -> {}:{}（target 需带 scheme，如 http://host:port）",
                self.log_prefix(),
                self.config.target_url, target_host, target_port
            ));
        }
        info(format!(
            "{}目标解析: {}:{} (from '{}')",
            self.log_prefix(),
            target_host,
            target_port,
            self.config.target_url
        ));
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
                        "{}连接已关闭，{:.1}秒后重连",
                        self.log_prefix(),
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
                        error(format!(
                            "{}超过最大重连次数 ({max})，停止",
                            self.log_prefix()
                        ));
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
                        "{}连接断开: {err}，{:.1}秒后重连 (第 {reconnect_count} 次, backoff={backoff_factor})",
                        self.log_prefix(),
                        delay as f32 / 1000.0
                    ));
                    self.sleep_interruptible(Duration::from_millis(delay)).await;
                }
            }
        }
    }

    /// 停止客户端（幂等；当前连接与重连等待都会尽快退出）
    ///
    /// 用 notify_one 而非 notify_waiters：notify_waiters 只唤醒「已存在的等待者」
    /// 且不存储许可，若 stop() 时恰好没有等待者（如刚进入下一轮循环的瞬间），
    /// 唤醒会整体丢失，Ctrl-C 后要等满整个 backoff 才退出。
    /// notify_one 会存储一个许可，保证后续任意一次 notified() 立即完成。
    /// 运行期同一时刻至多只有一个 notified() 等待者（select 循环或 sleep_interruptible），
    /// 单许可语义足够。
    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
        self.stop.notify_one();
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
        info(format!(
            "{}正在连接到 {}...",
            self.log_prefix(),
            self.config.server_url
        ));

        let (ws, _resp) = connect_async(&self.config.server_url)
            .await
            .map_err(|e| ConnectError::Io(format!("WebSocket 连接失败: {e}")))?;

        let (mut sink, mut stream) = ws.split();
        let (tx, mut rx) = mpsc::channel::<OutFrame>(TX_CHANNEL_CAPACITY);
        let writer = tokio::spawn(async move {
            while let Some(msg) = rx.recv().await {
                let ws_msg = match msg {
                    OutFrame::Text(t) => WsMessage::Text(t),
                    // 协议 v2 binary_frames：数据面 WS binary 帧
                    OutFrame::Binary(b) => WsMessage::Binary(b),
                };
                if sink.send(ws_msg).await.is_err() {
                    break;
                }
            }
            let _ = sink.close().await;
        });

        let mut session = Session {
            tx: tx.clone(),
            tcp: Arc::new(Mutex::new(HashMap::new())),
            udp: Arc::new(Mutex::new(HashMap::new())),
            http: reqwest::Client::builder()
                .build()
                .map_err(|e| ConnectError::Io(format!("http client: {e}")))?,
            target_base,
            target_host,
            target_port,
            request_timeout: self.config.request_timeout,
            binary_frames: false,
            udp_enabled: false,
        };

        // 发送认证（协议 v2：声明本客户端已实现的能力）
        tx.send(Message::auth(&self.config.token, force).into())
            .await
            .map_err(|_| ConnectError::Io("发送认证失败".into()))?;

        // keepalive：周期 ping + pong 看门狗。空闲长连接会被中间设备静默丢弃，
        // 客户端不发数据就永远感知不到——必须主动探测。
        let last_pong = Arc::new(Mutex::new(std::time::Instant::now()));
        let dead = Arc::new(Notify::new());
        let ticker = {
            let tx = tx.clone();
            let last_pong = last_pong.clone();
            let dead = dead.clone();
            let (interval, timeout) = (
                self.config.keepalive_interval,
                self.config.keepalive_timeout,
            );
            tokio::spawn(async move {
                let mut tick = tokio::time::interval(interval);
                tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
                tick.tick().await; // interval 首个 tick 立即返回，跳过
                loop {
                    tick.tick().await;
                    if last_pong.lock().await.elapsed() > timeout {
                        dead.notify_waiters();
                        break;
                    }
                    if tx.send(Message::Ping {}.into()).await.is_err() {
                        break;
                    }
                }
            })
        };

        let failure: Arc<Mutex<Option<ConnectError>>> = Arc::new(Mutex::new(None));

        loop {
            let next = tokio::select! {
                biased;
                _ = self.stop.notified() => None,
                _ = dead.notified() => {
                    warn("keepalive 超时：连接已静默死亡，重连");
                    *failure.lock().await = Some(ConnectError::Io("keepalive timeout".into()));
                    break;
                }
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
                WsMessage::Binary(b) => {
                    // 协议 v2 binary_frames：二进制消息 = 数据面帧（仅协商连接处理）；
                    // 未协商收到 binary → 丢弃 + warn；畸形帧 → 丢弃 + warn（F10 语义）
                    if !session.binary_frames {
                        warn("未协商 binary_frames，收到 WS 二进制消息已丢弃");
                        continue;
                    }
                    // 协议 v2 udp（T4）：帧类型 0x03 = udp_data 走 UDP 会话路径
                    // （另需该连接协商了 udp）；0x01 及其余按 tcp_data 解帧
                    if b.len() >= 2 && b[1] == FRAME_TYPE_UDP_DATA {
                        if !session.udp_enabled {
                            warn("未协商 udp，收到 udp_data 二进制帧已丢弃");
                            continue;
                        }
                        match decode_udp_data_frame(&b) {
                            Ok((session_id, payload)) => {
                                handle_udp_data_bytes(&session, &session_id, &payload).await
                            }
                            Err(e) => warn(format!("丢弃畸形 udp 帧: {e}")),
                        }
                        continue;
                    }
                    match decode_tcp_data_frame(&b) {
                        Ok((conn_id, payload)) => {
                            handle_tcp_data_bytes(&session, &conn_id, &payload).await
                        }
                        Err(e) => warn(format!("丢弃畸形 binary 帧: {e}")),
                    }
                    continue;
                }
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
                Message::AuthOk {
                    domain, capabilities, ..
                } => {
                    // 协议 v2：协商结果 = AuthOk.capabilities（缺字段 = 空 vec，
                    // 旧服务端不带该字段时全 JSON，行为与 0.7.x 一致）
                    session.binary_frames = capabilities.iter().any(|c| c == "binary_frames");
                    session.udp_enabled = capabilities.iter().any(|c| c == "udp");
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
                    let _ = session.tx.send(Message::pong().into()).await;
                }
                Message::Request {
                    id,
                    method,
                    path,
                    headers,
                    body,
                    timeout,
                    // 协议 v2 chunked_http（T3）：rust 不实现非 SSE 大响应
                    // 流式回传、也不声明该能力——即便收到 stream_ok=true
                    // 也照旧缓冲回 TunnelResponse（服务端补桥兜底）
                    stream_ok: _,
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
                Message::Pong {} => {
                    *last_pong.lock().await = std::time::Instant::now();
                }
                Message::TcpClose { conn_id, .. } => {
                    handle_server_tcp_close(&session, &conn_id).await
                }
                // 协议 v2 udp（T4）：会话建立 / 关闭
                Message::UdpOpen { session_id } => handle_udp_open(&session, &session_id).await,
                Message::UdpClose { session_id, .. } => {
                    handle_server_udp_close(&session, &session_id).await
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
        // UDP 会话同理回收（协议 v2 udp：socket 不跨连接复用，服务端重连后
        // 会以新 session_id 重建）
        cleanup_udp(&session).await;
        drop(tx);
        writer.abort();
        ticker.abort();

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
        let url = format!("{}{}", session.target_base, normalize_path(&path));
        let http_method = match reqwest::Method::from_bytes(method.as_bytes()) {
            Ok(m) => m,
            Err(_) => {
                // F18：非法 HTTP 方法不能静默变 GET——回退保留，但必须留下日志
                warn(format!(
                    "非法 HTTP 方法 '{method}'（request {id}），回退为 GET"
                ));
                reqwest::Method::GET
            }
        };

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
                    let _ = session
                        .tx
                        .send(Message::response(
                            &id,
                            status,
                            resp_headers,
                            Some(body),
                            None,
                            duration,
                        )
                        .into())
                        .await;
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
                let _ = session
                    .tx
                    .send(Message::response(
                        &id,
                        status,
                        HashMap::new(),
                        None,
                        Some(error),
                        duration,
                    )
                    .into())
                    .await;
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
    let _ = session
        .tx
        .send(Message::stream_start(id, status, headers).into())
        .await;

    let mut decoder = crate::protocol::Utf8StreamDecoder::new();
    let mut seq: u32 = 0;
    let mut error_msg: Option<String> = None;

    loop {
        match resp.chunk().await {
            Ok(Some(chunk)) => {
                let text = decoder.decode(&chunk);
                if !text.is_empty() {
                    let _ = session.tx.send(Message::stream_chunk(id, text, seq).into()).await;
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

    // 冲洗缓存的未完成多字节序列（以 U+FFFD 收尾，WHATWG 替换语义）
    let tail = decoder.flush();
    if !tail.is_empty() {
        let _ = session.tx.send(Message::stream_chunk(id, tail, seq).into()).await;
        seq += 1;
    }

    let duration = start.elapsed().as_millis() as u64;
    let _ = session
        .tx
        .send(Message::stream_end(id, error_msg, duration, seq).into())
        .await;
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
                .send(Message::tcp_close(conn_id, Some(e.to_string())).into())
                .await;
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
                // 协议 v2 binary_frames：协商了就组二进制帧直发（去 base64+JSON
                // 开销）；否则走 0.7.x 的 JSON+base64 路径（wire 不变）
                let outbound = if session.binary_frames {
                    match encode_tcp_data_frame(conn_id, &buf[..n]) {
                        Ok(frame) => OutFrame::Binary(frame),
                        // conn_id 非 UUID 等极端情况：回落 JSON 帧，不丢数据
                        Err(_) => OutFrame::from(json_tcp_data(conn_id, &buf[..n], seq)),
                    }
                } else {
                    OutFrame::from(json_tcp_data(conn_id, &buf[..n], seq))
                };
                if session.tx.send(outbound).await.is_err() {
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
        let _ = session.tx.send(Message::tcp_close(conn_id, error).into()).await;
    }
    {
        let mut w = conn.write.lock().await;
        let _ = w.shutdown().await;
    }
    session.tcp.lock().await.remove(conn_id);
}

/// JSON 形态的 tcp_data 消息（0.7.x wire：base64 + sequence）
fn json_tcp_data(conn_id: &str, data: &[u8], seq: u32) -> Message {
    let encoded = base64::engine::general_purpose::STANDARD.encode(data);
    Message::tcp_data(conn_id, &encoded, seq)
}

/// 处理来自服务端的 TCP 数据（JSON 形态，base64）：解码后与 binary 帧路径
/// 共用 handle_tcp_data_bytes 落地。
async fn handle_tcp_data(session: &Session, conn_id: &str, data: &str) {
    let bytes = match base64::engine::general_purpose::STANDARD.decode(data) {
        Ok(b) => b,
        Err(e) => {
            error(format!("TCP 数据 base64 解码错误: {conn_id}, {e}"));
            return;
        }
    };
    handle_tcp_data_bytes(session, conn_id, &bytes).await;
}

/// tcp_data 落地写入本地连接（JSON 与 binary 帧两路共用）
async fn handle_tcp_data_bytes(session: &Session, conn_id: &str, bytes: &[u8]) {
    let conn = session.tcp.lock().await.get(conn_id).cloned();
    let Some(conn) = conn else {
        // 在途数据竞到本地关闭之后（并发关闭竞态的正常时序）
        debug(format!("收到未知 TCP 连接的数据: {conn_id}"));
        return;
    };
    use tokio::io::AsyncWriteExt;
    let mut w = conn.write.lock().await;
    let _ = w.write_all(bytes).await; // 写失败会随后以 read 侧 EOF/error 收敛
}

async fn handle_server_tcp_close(session: &Session, conn_id: &str) {
    let conn = session.tcp.lock().await.remove(conn_id);
    let Some(conn) = conn else {
        // 服务端迟到帧（本端已先关闭/已收到过 close），并发关闭竞态的正常时序
        debug(format!("尝试关闭未知 TCP 连接: {conn_id}"));
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

// ============== UDP 模式（协议 v2 udp） ==============

/// UDP 会话接收缓冲区大小：对齐主流 MTU 上限之上的裕量（jumbogram 罕见，
/// 更大数据报会被截断——UDP 语义下可接受）
const UDP_RECV_BUF_SIZE: usize = 65536;

/// target 解析可疑判定：解析结果落回默认值而配置串并不是明示的 localhost:8080
fn session_target_looks_suspicious(target_url: &str, host: &str, port: u16) -> bool {
    host == "localhost" && port == 8080 && !target_url.contains("localhost")
}

/// 处理 udp_open（服务端 → 客户端）：建到目标的 UDP socket（每会话一个）
/// + 起收包任务（回包组 0x03 帧回服务端）。
async fn handle_udp_open(session: &Session, session_id: &str) {
    // 能力门控：未协商 udp 不该收到 udp_open（防御：丢弃）
    if !session.udp_enabled {
        warn("未协商 udp，忽略 udp_open");
        return;
    }
    {
        let udp = session.udp.lock().await;
        if udp.contains_key(session_id) {
            warn(format!("UDP 会话已存在，忽略重复的 udp_open: {session_id}"));
            return;
        }
    }

    let socket = match UdpSocket::bind("127.0.0.1:0").await {
        Ok(s) => Arc::new(s),
        Err(e) => {
            error(format!(
                "UDP bind 失败: {session_id} -> {}:{}, {e}",
                session.target_host, session.target_port
            ));
            let _ = session
                .tx
                .send(Message::udp_close(
                    session_id,
                    Some(format!("bind failed: {e}")),
                )
                .into())
                .await;
            return;
        }
    };
    if let Err(e) = socket
        .connect((session.target_host.as_str(), session.target_port))
        .await
    {
        error(format!(
            "UDP connect 失败: {session_id} -> {}:{}, {e}",
            session.target_host, session.target_port
        ));
        let _ = session
            .tx
            .send(
                Message::udp_close(session_id, Some(format!("connect failed: {e}"))).into(),
            )
            .await;
        return;
    }

    // 先入表再启动收包任务，任务起来后整条替换表项（Arc 不可变，替换而非改写）：
    // 若任务瞬间出错自行移除了表项，ptr_eq 不匹配则不回插——无泄漏无竞争
    let conn = Arc::new(UdpConn {
        socket: socket.clone(),
        recv_task: tokio::spawn(async {}),
    });
    session
        .udp
        .lock()
        .await
        .insert(session_id.to_string(), conn.clone());

    let recv_session = session.clone();
    let recv_id = session_id.to_string();
    let task_socket = socket.clone();
    let task = tokio::spawn(async move {
        udp_recv_loop(&recv_session, &recv_id, task_socket).await;
    });
    {
        let mut udp = session.udp.lock().await;
        let is_current = udp.get(session_id).map(|c| Arc::ptr_eq(c, &conn)) == Some(true);
        if is_current {
            udp.insert(
                session_id.to_string(),
                Arc::new(UdpConn {
                    socket,
                    recv_task: task,
                }),
            );
        }
    }
    info(format!(
        "UDP 会话已建立: {session_id} -> {}:{}",
        session.target_host, session.target_port
    ));
}

/// 目标 → 服务端方向收包循环：回包组 0x03 帧回发；socket 错误/关闭时
/// 清理会话并回执 udp_close（socket 生命周期随任务结束 drop）。
async fn udp_recv_loop(session: &Session, session_id: &str, socket: Arc<UdpSocket>) {
    let mut buf = vec![0u8; UDP_RECV_BUF_SIZE];
    loop {
        match socket.recv(&mut buf).await {
            Ok(0) => {
                finish_udp(session, session_id, Some("socket closed".into())).await;
                break;
            }
            Ok(n) => {
                let outbound = match encode_udp_data_frame(session_id, &buf[..n]) {
                    Ok(frame) => OutFrame::Binary(frame),
                    // session_id 非 UUID 等极端情况：丢帧告警（无 JSON 回落变体——
                    // PROTOCOL_V2 §5：协商了 udp 就必然有 binary frame 通道）
                    Err(e) => {
                        warn(format!("udp_data 帧编码失败（丢弃）: {session_id}, {e}"));
                        continue;
                    }
                };
                if session.tx.send(outbound).await.is_err() {
                    // WebSocket 已断：本地清理（udp_close 发不出去了）
                    session.udp.lock().await.remove(session_id);
                    break;
                }
            }
            Err(e) => {
                warn(format!("UDP 收包错误: {session_id}, {e}"));
                finish_udp(session, session_id, Some(e.to_string())).await;
                break;
            }
        }
    }
}

/// 本地侧结束（EOF/error）：从会话表移除并回执 udp_close（幂等——
/// 表里已不是本会话时不再发，服务端主动关闭场景由调用方静默处理）
async fn finish_udp(session: &Session, session_id: &str, reason: Option<String>) {
    let removed = session.udp.lock().await.remove(session_id);
    if removed.is_some() {
        let _ = session
            .tx
            .send(Message::udp_close(session_id, reason).into())
            .await;
    }
}

/// 处理服务端 → 客户端方向的 udp_data 落地：写入会话的目标 socket
async fn handle_udp_data_bytes(session: &Session, session_id: &str, payload: &[u8]) {
    let conn = session.udp.lock().await.get(session_id).cloned();
    let Some(conn) = conn else {
        // 会话已被回收/尚未建立：丢弃（UDP 无连接语义；对齐 TCP 侧未知连接告警）
        warn(format!("收到未知 UDP 会话的数据: {session_id}"));
        return;
    };
    // sendto 连接型 socket；ICMP 不可达等错误会在 recv 侧以 Err 收敛并触发会话清理
    let _ = conn.socket.send(payload).await;
}

/// 服务端主动关闭 UDP 会话（空闲超时回收等）：abort 读任务、关 socket，
/// 不回执 udp_close
async fn handle_server_udp_close(session: &Session, session_id: &str) {
    let conn = session.udp.lock().await.remove(session_id);
    let Some(conn) = conn else {
        warn(format!("尝试关闭未知 UDP 会话: {session_id}"));
        return;
    };
    conn.recv_task.abort();
    info(format!("关闭 UDP 会话: {session_id}（服务端发起）"));
}

/// 断连收尾：回收全部 UDP 会话（abort 读任务 + drop socket），不回执
/// （WS 已断，udp_close 发不到服务端；重连后服务端以新 session_id 重建）
async fn cleanup_udp(session: &Session) {
    let mut udp = session.udp.lock().await;
    for (_, conn) in udp.drain() {
        conn.recv_task.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::normalize_path;

    #[test]
    fn normalize_path_prefixes_path_starting_with_at() {
        // "@evil/x" 若直接拼接会改写 URL authority（SSRF）
        assert_eq!(normalize_path("@evil/x"), "/@evil/x");
    }

    #[test]
    fn normalize_path_keeps_absolute_path() {
        assert_eq!(normalize_path("/ok"), "/ok");
    }

    #[test]
    fn normalize_path_empty_becomes_root() {
        assert_eq!(normalize_path(""), "/");
    }

    // ============== UDP 会话生命周期（协议 v2 udp，协议 0.9.0 T4） ==============
    //
    // Session 直构（tx 通道 + 空 conn 表），驱动 handle_udp_open /
    // handle_udp_data_bytes / handle_server_udp_close / cleanup_udp 验证
    // 会话表生命周期与回执 wire；数据面用真实 UDP socket 对打。

    use std::sync::Arc;

    use super::{cleanup_udp, handle_server_udp_close, handle_udp_data_bytes, handle_udp_open};
    use crate::client::Session;
    use crate::protocol::{
        decode_udp_data_frame, encode_udp_data_frame, Message, FRAME_TYPE_UDP_DATA,
    };

    fn test_session(udp_enabled: bool, target_port: u16) -> (Session, tokio::sync::mpsc::Receiver<super::OutFrame>) {
        let (tx, rx) = tokio::sync::mpsc::channel(64);
        let session = Session {
            tx,
            tcp: Arc::new(tokio::sync::Mutex::new(std::collections::HashMap::new())),
            udp: Arc::new(tokio::sync::Mutex::new(std::collections::HashMap::new())),
            http: reqwest::Client::new(),
            target_base: "http://127.0.0.1".into(),
            target_host: "127.0.0.1".into(),
            target_port,
            request_timeout: std::time::Duration::from_secs(5),
            binary_frames: true,
            udp_enabled,
        };
        (session, rx)
    }

    fn udp_open_json(session_id: &str) -> String {
        Message::parse(&format!(
            r#"{{"type":"udp_open","session_id":"{session_id}","timestamp":"now"}}"#
        ))
        .unwrap()
        .to_json()
    }

    #[tokio::test]
    async fn udp_open_creates_socket_and_frame_routes_to_target() {
        // 目标 echo socket：收到的数据报原样回发
        let echo = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let echo_addr = echo.local_addr().unwrap();
        tokio::spawn(async move {
            let mut buf = [0u8; 2048];
            loop {
                if let Ok((n, peer)) = echo.recv_from(&mut buf).await {
                    let _ = echo.send_to(&buf[..n], peer).await;
                }
            }
        });

        let (session, mut rx) = test_session(true, echo_addr.port());
        let session_id = "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b";

        // 服务端视角的 udp_open JSON（带 timestamp 冗余字段）先可解析
        assert!(crate::protocol::Message::parse(&udp_open_json(session_id)).is_ok());

        handle_udp_open(&session, session_id).await;
        assert!(
            session.udp.lock().await.contains_key(session_id),
            "udp_open 后会话应已入表"
        );

        // 服务端 → 客户端 0x03 帧 → 写入目标 socket → echo 回发 → 收包任务回帧
        let frame = encode_udp_data_frame(session_id, b"ping-udp").unwrap();
        handle_udp_data_bytes(&session, session_id, &frame[18..]).await;

        let outbound = tokio::time::timeout(std::time::Duration::from_secs(3), rx.recv())
            .await
            .expect("未收到回程帧")
            .expect("发送通道已关闭");
        let super::OutFrame::Binary(bytes) = outbound else {
            panic!("回程应为 Binary 帧");
        };
        assert_eq!(bytes[1], FRAME_TYPE_UDP_DATA);
        let (out_id, payload) = decode_udp_data_frame(&bytes).unwrap();
        assert_eq!(out_id, session_id);
        assert_eq!(payload, b"ping-udp");
    }

    #[tokio::test]
    async fn udp_open_ignored_when_not_negotiated() {
        let (session, mut rx) = test_session(false, 9);
        handle_udp_open(&session, "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b")
            .await;
        assert!(
            session.udp.lock().await.is_empty(),
            "未协商 udp 不得建会话"
        );
        // 不产生任何回执
        assert!(rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn udp_open_duplicate_id_ignored() {
        let bind = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = bind.local_addr().unwrap();
        let (session, _rx) = test_session(true, addr.port());
        let session_id = "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b";
        handle_udp_open(&session, session_id).await;
        let before = session.udp.lock().await.len();
        handle_udp_open(&session, session_id).await;
        assert_eq!(session.udp.lock().await.len(), before, "重复 udp_open 不得新条目");
    }

    #[tokio::test]
    async fn udp_data_unknown_session_dropped() {
        let (session, mut rx) = test_session(true, 9);
        handle_udp_data_bytes(&session, "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b", b"orphan").await;
        // 未知会话：无回执、无 panic
        assert!(rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn server_udp_close_removes_and_aborts_recv_task() {
        let bind = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = bind.local_addr().unwrap();
        let (session, mut rx) = test_session(true, addr.port());
        let session_id = "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b";
        handle_udp_open(&session, session_id).await;

        handle_server_udp_close(&session, session_id).await;
        assert!(session.udp.lock().await.is_empty(), "关闭后会话应出表");
        // 服务端主动关闭：客户端不回执
        assert!(rx.try_recv().is_err());

        // 重复关闭：告警但无害
        handle_server_udp_close(&session, session_id).await;
    }

    #[tokio::test]
    async fn socket_error_finishes_session_with_udp_close() {
        // 向未监听的本地端口发包触发 ICMP 不可达 → recv 侧 Err → 会话清理 + udp_close
        let dead = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let dead_addr = dead.local_addr().unwrap();
        drop(dead); // 端口无人监听

        let (session, mut rx) = test_session(true, dead_addr.port());
        let session_id = "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b";
        handle_udp_open(&session, session_id).await;

        // 触发 sendto（错误在 recv 侧收敛）
        handle_udp_data_bytes(&session, session_id, b"to-dead-port").await;

        // 等 udp_close 回执（ICMP 不可达为异步路径，轮询等待）
        let mut got_close = false;
        for _ in 0..100 {
            if let Ok(Some(super::OutFrame::Text(json))) =
                tokio::time::timeout(std::time::Duration::from_millis(100), rx.recv()).await
            {
                if json.contains(r#""type":"udp_close""#) && json.contains(session_id) {
                    got_close = true;
                    break;
                }
            }
        }
        assert!(got_close, "socket 错误后应回执 udp_close");
        assert!(
            !session.udp.lock().await.contains_key(session_id),
            "socket 错误后会话应出表"
        );
    }

    #[tokio::test]
    async fn cleanup_udp_clears_all_sessions() {
        let bind = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = bind.local_addr().unwrap();
        let (session, _rx) = test_session(true, addr.port());
        handle_udp_open(&session, "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b").await;
        handle_udp_open(&session, "00112233-4455-4677-8899-aabbccddeeff").await;
        assert_eq!(session.udp.lock().await.len(), 2);

        cleanup_udp(&session).await;
        assert!(session.udp.lock().await.is_empty(), "断连收尾应清空会话表");
    }
}
