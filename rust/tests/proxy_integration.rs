//! 集成测试：客户端出站经 HTTP CONNECT mock 代理的全环验证。
//!
//! 拓扑：TunnelClient → mock CONNECT 代理（校验请求行/头） → FakeServer（WS） → auth → 隧道连通。
//! 覆盖：
//! 1. 配置 proxy 时 CONNECT 请求行字节级正确（`CONNECT host:port HTTP/1.1`）且
//!    隧道建立后 auth 照常到达服务端（auth_ok 正常下发）；
//! 2. 经代理的完整隧道环：tcp_connect / tcp_data 双向转发照常工作；
//! 3. 未配置 proxy 的直连路径不受影响（行为回归）。

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, Mutex};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::{accept_async, WebSocketStream};
use tunely::client::{TunnelClient, TunnelClientConfig};

const RECV_TIMEOUT: Duration = Duration::from_secs(3);

// ============== mock CONNECT 代理 ==============

/// 记录代理看到的首行 CONNECT 请求（诊断断言用）
#[derive(Default, Clone)]
struct ProxyLog {
    connect_count: Arc<AtomicUsize>,
    first_request_line: Arc<StdMutex<Option<String>>>,
}

impl ProxyLog {
    fn request_line(&self) -> String {
        self.first_request_line
            .lock()
            .unwrap()
            .clone()
            .unwrap_or_default()
    }
}

/// 起一个最小 CONNECT 代理：
/// 读入请求头 → 记录请求行 → 从 `CONNECT host:port` 解析目标 → 连目标 →
/// 回 `200 Connection established` → 双向裸搬运（对客户端与目标完全透明）。
async fn start_mock_proxy() -> (std::net::SocketAddr, ProxyLog) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let log = ProxyLog::default();
    let log2 = log.clone();
    tokio::spawn(async move {
        loop {
            let Ok((client, _)) = listener.accept().await else {
                break;
            };
            let log = log2.clone();
            tokio::spawn(async move {
                if let Err(e) = handle_proxy_conn(client, log).await {
                    eprintln!("mock 代理连接处理失败: {e}");
                }
            });
        }
    });
    (addr, log)
}

async fn handle_proxy_conn(mut client: TcpStream, log: ProxyLog) -> std::io::Result<()> {
    // 逐字节读到头部分隔符（与客户端实现同规则：不吞隧道内首包）
    let mut head = Vec::with_capacity(256);
    let mut byte = [0u8; 1];
    loop {
        let n = client.read(&mut byte).await?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "请求头未读完连接即关闭",
            ));
        }
        head.push(byte[0]);
        if head.ends_with(b"\r\n\r\n") {
            break;
        }
    }

    let head_text = String::from_utf8_lossy(&head);
    let request_line = head_text.lines().next().unwrap_or("").to_string();
    {
        let mut first = log.first_request_line.lock().unwrap();
        if first.is_none() {
            *first = Some(request_line.clone());
        }
    }

    // 解析 CONNECT 目标：CONNECT host:port HTTP/1.1
    let mut parts = request_line.split(' ');
    let method = parts.next().unwrap_or("");
    let authority = parts.next().unwrap_or("");
    assert_eq!(method, "CONNECT", "mock 代理只处理 CONNECT: {request_line}");
    let (host, port) = authority
        .rsplit_once(':')
        .expect("CONNECT authority 应为 host:port");
    let port: u16 = port.parse().expect("CONNECT 端口应为数字");

    let upstream = TcpStream::connect((host, port)).await?;
    client
        .write_all(b"HTTP/1.1 200 Connection established\r\n\r\n")
        .await?;
    log.connect_count.fetch_add(1, Ordering::SeqCst);

    // 隧道建立：双向裸搬运直至任一侧关闭
    let (mut cr, mut cw) = client.into_split();
    let (mut ur, mut uw) = upstream.into_split();
    let to_upstream = tokio::spawn(async move {
        let mut buf = vec![0u8; 65536];
        loop {
            match cr.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if uw.write_all(&buf[..n]).await.is_err() {
                        break;
                    }
                }
            }
        }
        let _ = uw.shutdown().await;
    });
    let to_client = tokio::spawn(async move {
        let mut buf = vec![0u8; 65536];
        loop {
            match ur.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if cw.write_all(&buf[..n]).await.is_err() {
                        break;
                    }
                }
            }
        }
        let _ = cw.shutdown().await;
    });
    let _ = tokio::join!(to_upstream, to_client);
    Ok(())
}

// ============== 假隧道服务端（与 tests/integration.rs 同型） ==============

struct FakeServer {
    addr: std::net::SocketAddr,
    rx: mpsc::UnboundedReceiver<FakeConn>,
}

struct FakeConn {
    sink: futures_util::stream::SplitSink<WebSocketStream<TcpStream>, WsMessage>,
    stream: futures_util::stream::SplitStream<WebSocketStream<TcpStream>>,
}

impl FakeConn {
    async fn next_message(&mut self) -> Value {
        let msg = tokio::time::timeout(RECV_TIMEOUT, async {
            loop {
                match self.stream.next().await {
                    Some(Ok(WsMessage::Text(t))) => break Some(t),
                    Some(Ok(_)) => continue,
                    Some(Err(e)) => panic!("ws 错误: {e}"),
                    None => break None,
                }
            }
        })
        .await
        .expect("等待客户端消息超时")
        .expect("服务端 channel 关闭");
        serde_json::from_str(&msg).expect("合法 JSON")
    }

    async fn send(&mut self, v: Value) {
        self.sink
            .send(WsMessage::Text(v.to_string()))
            .await
            .expect("发送给客户端失败");
    }

    /// 读认证消息，返回 (token, force)
    async fn expect_auth(&mut self) -> (String, bool) {
        let msg = self.next_message().await;
        assert_eq!(msg["type"], "auth", "首条消息应为 auth: {msg}");
        (
            msg["token"].as_str().unwrap().to_string(),
            msg["force"].as_bool().unwrap_or(false),
        )
    }

    async fn send_auth_ok(&mut self, domain: &str) {
        self.send(json!({"type": "auth_ok", "domain": domain, "tunnel_id": "t-1"}))
            .await;
    }
}

impl FakeServer {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = mpsc::unbounded_channel();
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    break;
                };
                let ws = accept_async(stream).await.expect("ws 握手失败");
                let (sink, stream) = ws.split();
                if tx.send(FakeConn { sink, stream }).is_err() {
                    break;
                }
            }
        });
        Self { addr, rx }
    }

    async fn next_conn(&mut self) -> FakeConn {
        tokio::time::timeout(RECV_TIMEOUT, self.rx.recv())
            .await
            .expect("等待客户端连入超时")
            .expect("服务端 channel 关闭")
    }

    fn ws_url(&self) -> String {
        format!("ws://{}", self.addr)
    }
}

// ============== 本地 echo 目标（隧道连通性用） ==============

async fn start_echo_target() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        loop {
            let Ok((socket, _)) = listener.accept().await else {
                break;
            };
            tokio::spawn(async move {
                let (mut r, mut w) = socket.into_split();
                let mut buf = vec![0u8; 65536];
                loop {
                    match r.read(&mut buf).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            if w.write_all(&buf[..n]).await.is_err() {
                                break;
                            }
                        }
                    }
                }
            });
        }
    });
    port
}

// ============== 客户端启动 ==============

struct ClientHandle {
    client: Arc<TunnelClient>,
    task: JoinHandle<()>,
    connect_rx: mpsc::UnboundedReceiver<String>,
}

async fn spawn_client(server_url: &str, target_url: &str, proxy: Option<String>) -> ClientHandle {
    let client = Arc::new(TunnelClient::new(TunnelClientConfig {
        server_url: server_url.to_string(),
        token: "test-token".to_string(),
        target_url: target_url.to_string(),
        reconnect_interval: Duration::from_millis(50),
        proxy,
        ..Default::default()
    }));
    let (ctx, crx) = mpsc::unbounded_channel();
    client.on_connect(move |d| {
        let _ = ctx.send(d.to_string());
    });
    let task = tokio::spawn({
        let c = client.clone();
        async move { c.run().await }
    });
    ClientHandle {
        client,
        task,
        connect_rx: crx,
    }
}

impl ClientHandle {
    async fn expect_connected(&mut self, domain: &str) {
        let d = tokio::time::timeout(RECV_TIMEOUT, self.connect_rx.recv())
            .await
            .expect("等待 on_connect 超时")
            .expect("channel 关闭");
        assert_eq!(d, domain);
    }

    async fn stop(self) {
        self.client.stop();
        let _ = tokio::time::timeout(RECV_TIMEOUT, self.task).await;
    }
}

// ============== 测试 ==============

#[tokio::test]
async fn proxied_client_reaches_server_via_connect_tunnel() {
    // 全环：客户端 → mock CONNECT 代理 → FakeServer → auth 成功
    let (proxy_addr, proxy_log) = start_mock_proxy().await;
    let mut server = FakeServer::start().await;
    let mut h = spawn_client(
        &server.ws_url(),
        "http://127.0.0.1:1",
        Some(format!("http://{proxy_addr}")),
    )
    .await;

    // 代理侧：恰好一次 CONNECT，请求行字节级正确（host:port 取自 server URL）
    let mut conn = server.next_conn().await;
    assert_eq!(
        proxy_log.request_line(),
        format!("CONNECT {} HTTP/1.1", server.addr),
        "CONNECT 请求行应为 server URL 的 authority"
    );

    // 服务端侧：auth 照常到达（CONNECT 隧道内的 WS 握手 + 认证全通）
    let (token, force) = conn.expect_auth().await;
    assert_eq!(token, "test-token");
    assert!(!force);
    conn.send_auth_ok("via-proxy").await;
    h.expect_connected("via-proxy").await;

    h.stop().await;
    assert_eq!(proxy_log.connect_count.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn proxied_tunnel_end_to_end_tcp_echo() {
    // 全环 + 隧道连通：auth 后 tcp_connect/tcp_data 经代理照常双向转发
    let (proxy_addr, proxy_log) = start_mock_proxy().await;
    let mut server = FakeServer::start().await;
    let echo_port = start_echo_target().await;
    let mut h = spawn_client(
        &server.ws_url(),
        &format!("http://127.0.0.1:{echo_port}"),
        Some(format!("http://{proxy_addr}")),
    )
    .await;

    let mut conn = server.next_conn().await;
    let _ = conn.expect_auth().await;
    conn.send_auth_ok("via-proxy").await;
    h.expect_connected("via-proxy").await;

    // 隧道连通验证：服务端下发 tcp_connect → 客户端连本地 echo → 数据回声
    conn.send(json!({"type": "tcp_connect", "conn_id": "c-proxy"}))
        .await;
    use base64::Engine as _;
    conn.send(json!({
        "type": "tcp_data",
        "conn_id": "c-proxy",
        "data": base64::engine::general_purpose::STANDARD.encode(b"hello-through-proxy"),
        "sequence": 0,
    }))
    .await;

    let msg = tokio::time::timeout(RECV_TIMEOUT, async {
        loop {
            let m = conn.next_message().await;
            if m["type"] == "tcp_data" {
                break m;
            }
        }
    })
    .await
    .expect("等待回声 tcp_data 超时");
    assert_eq!(msg["conn_id"], "c-proxy");
    let payload = base64::engine::general_purpose::STANDARD
        .decode(msg["data"].as_str().unwrap())
        .unwrap();
    assert_eq!(payload, b"hello-through-proxy");

    h.stop().await;
    assert_eq!(proxy_log.connect_count.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn direct_connect_path_unaffected_when_proxy_none() {
    // 行为回归：不配置 proxy 时直连路径不变（不会误连代理）
    let (_proxy_addr, proxy_log) = start_mock_proxy().await; // 起着但没人该连它
    let mut server = FakeServer::start().await;
    let mut h = spawn_client(&server.ws_url(), "http://127.0.0.1:1", None).await;

    let mut conn = server.next_conn().await;
    let _ = conn.expect_auth().await;
    conn.send_auth_ok("direct").await;
    h.expect_connected("direct").await;

    h.stop().await;
    assert_eq!(
        proxy_log.connect_count.load(Ordering::SeqCst),
        0,
        "直连路径不应触碰代理"
    );
}

#[tokio::test]
async fn proxied_client_fails_fast_and_retries_when_proxy_refuses() {
    // 代理端口拒绝：连接失败走既有重连/退避语义（on_error 触发），不 panic 不挂死
    let mut server = FakeServer::start().await;
    // 占一个端口再释放 → 确定拒绝连接的代理地址
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let dead_proxy = l.local_addr().unwrap();
    drop(l);

    let client = Arc::new(TunnelClient::new(TunnelClientConfig {
        server_url: server.ws_url(),
        token: "test-token".to_string(),
        target_url: "http://127.0.0.1:1".to_string(),
        reconnect_interval: Duration::from_millis(50),
        max_reconnect_attempts: 1,
        proxy: Some(format!("http://{dead_proxy}")),
        ..Default::default()
    }));
    let errors: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    {
        let errors = errors.clone();
        client.on_error(move |msg| {
            if let Ok(mut g) = errors.try_lock() {
                g.push(msg.to_string());
            }
        });
    }
    let task = tokio::spawn({
        let c = client.clone();
        async move { c.run().await }
    });

    // 等到出现代理失败错误即可（重连一次后耗尽 max_reconnect 退出）
    let mut saw_proxy_error = false;
    for _ in 0..100 {
        if errors.lock().await.iter().any(|e| e.contains("代理 CONNECT 失败")) {
            saw_proxy_error = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(saw_proxy_error, "应报告代理 CONNECT 失败而非泛化网络错误");
    client.stop();
    let _ = tokio::time::timeout(RECV_TIMEOUT, task).await;

    // 服务端全程不应收到任何连接
    assert!(
        tokio::time::timeout(Duration::from_millis(200), server.rx.recv())
            .await
            .is_err(),
        "代理不可达时不应有连接到达服务端"
    );
}
