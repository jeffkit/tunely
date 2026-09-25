//! 集成测试：客户端 vs 假隧道服务端（真实 WebSocket）+ 真实本地 TCP/HTTP 目标服务。
//!
//! 覆盖 issue #1 验收语义在 Rust 客户端的等价实现：
//! 认证（含 force 抢占）、ping/pong、TCP 双向转发、sequence、目标拒绝、
//! 幂等 tcp_close、服务端关闭不回执、WS 断开清理 + 重连、HTTP 转发、SSE 流式。

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::{accept_async, WebSocketStream};
use tunely::client::{TunnelClient, TunnelClientConfig};

const RECV_TIMEOUT: Duration = Duration::from_secs(3);

// ============== 假隧道服务端 ==============

struct FakeServer {
    addr: std::net::SocketAddr,
    rx: mpsc::UnboundedReceiver<FakeConn>,
}

struct FakeConn {
    sink: futures_util::stream::SplitSink<WebSocketStream<TcpStream>, WsMessage>,
    stream: futures_util::stream::SplitStream<WebSocketStream<TcpStream>>,
}

impl FakeConn {
    /// 读到第一条消息（跳过非 Text），返回 JSON
    async fn next_message(&mut self) -> Value {
        self.try_next_message(RECV_TIMEOUT)
            .await
            .expect("等待客户端消息超时")
    }

    async fn try_next_message(&mut self, timeout: Duration) -> Option<Value> {
        let deadline = tokio::time::timeout(timeout, async {
            loop {
                match self.stream.next().await {
                    Some(Ok(WsMessage::Text(t))) => {
                        break Some(serde_json::from_str(&t).expect("合法 JSON"));
                    }
                    Some(Ok(_)) => continue,
                    Some(Err(e)) => panic!("ws 错误: {e}"),
                    None => break None,
                }
            }
        })
        .await;
        match deadline {
            Ok(v) => v,
            Err(_) => None,
        }
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
        self.send(json!({"type": "auth_ok", "domain": domain, "tunnel_id": "t-1"})).await;
    }

    async fn send_tcp_connect(&mut self, conn_id: &str) {
        self.send(json!({"type": "tcp_connect", "conn_id": conn_id})).await;
    }

    async fn send_tcp_data(&mut self, conn_id: &str, payload: &[u8], seq: u32) {
        use base64::Engine as _;
        self.send(json!({
            "type": "tcp_data",
            "conn_id": conn_id,
            "data": base64::engine::general_purpose::STANDARD.encode(payload),
            "sequence": seq,
        }))
        .await;
    }

    /// 断言收到客户端的 tcp_data，返回 (payload, sequence)
    async fn expect_tcp_data(&mut self, conn_id: &str) -> (Vec<u8>, u32) {
        use base64::Engine as _;
        let msg = self.next_message().await;
        assert_eq!(msg["type"], "tcp_data", "应为 tcp_data: {msg}");
        assert_eq!(msg["conn_id"], conn_id, "conn_id 应原样回带: {msg}");
        let data = base64::engine::general_purpose::STANDARD
            .decode(msg["data"].as_str().unwrap_or_else(|| panic!("tcp_data 缺 data 字段: {msg}")))
            .unwrap();
        (data, msg["sequence"].as_u64().unwrap_or(0) as u32)
    }

    async fn expect_tcp_close(&mut self, conn_id: &str) -> Option<String> {
        let msg = self.next_message().await;
        assert_eq!(msg["type"], "tcp_close", "应为 tcp_close: {msg}");
        assert_eq!(msg["conn_id"], conn_id);
        msg["error"].as_str().map(String::from)
    }
}

impl FakeServer {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = mpsc::unbounded_channel();
        tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
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
        let a = self.addr;
        format!("ws://{a}")
    }
}

// ============== 本地目标服务 ==============

#[derive(Clone)]
struct TargetStats {
    connections: Arc<AtomicUsize>,
    closed: Arc<AtomicUsize>,
}

impl TargetStats {
    fn new() -> Self {
        Self {
            connections: Arc::new(AtomicUsize::new(0)),
            closed: Arc::new(AtomicUsize::new(0)),
        }
    }
    fn conn_count(&self) -> usize {
        self.connections.load(Ordering::SeqCst)
    }
    async fn wait_closed(&self, n: usize) {
        for _ in 0..100 {
            if self.closed.load(Ordering::SeqCst) >= n {
                return;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        panic!("等待 {n} 个连接关闭超时");
    }
}

/// 启动回环 echo 目标。close_after_echo=true 时回写一次后立即关闭连接。
async fn start_echo_target(close_after_echo: bool) -> (u16, TargetStats) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let stats = TargetStats::new();
    let stats2 = stats.clone();
    tokio::spawn(async move {
        loop {
            let Ok((socket, _)) = listener.accept().await else { break };
            stats2.connections.fetch_add(1, Ordering::SeqCst);
            let stats3 = stats2.clone();
            tokio::spawn(async move {
                let (mut r, mut w) = socket.into_split();
                use tokio::io::{AsyncReadExt, AsyncWriteExt};
                let mut buf = vec![0u8; 65536];
                loop {
                    match r.read(&mut buf).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            if w.write_all(&buf[..n]).await.is_err() {
                                break;
                            }
                            if close_after_echo {
                                let _ = w.shutdown().await;
                                break;
                            }
                        }
                    }
                }
                stats3.closed.fetch_add(1, Ordering::SeqCst);
            });
        }
    });
    (port, stats)
}

/// 启动极简 HTTP 目标：mode="text" 返回 200 纯文本；mode="sse" 返回 2 条 SSE 事件（chunked）
async fn start_http_target(mode: &str) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let mode = mode.to_string();
    tokio::spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else { break };
            let mode = mode.clone();
            tokio::spawn(async move {
                use tokio::io::{AsyncReadExt, AsyncWriteExt};
                let mut buf = vec![0u8; 8192];
                let _ = socket.read(&mut buf).await; // 读掉请求头即可
                let resp = match mode.as_str() {
                    "sse" => {
                        let body = "data: msg-0\n\ndata: msg-1\n\n";
                        format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\
                             Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:x}\r\n{}\r\n0\r\n\r\n",
                            body.len(),
                            body
                        )
                    }
                    _ => "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 14\r\nConnection: close\r\n\r\nok-from-target"
                        .to_string(),
                };
                let _ = socket.write_all(resp.as_bytes()).await;
                let _ = socket.shutdown().await;
            });
        }
    });
    port
}

/// 拿一个确定空闲的端口（先绑定再释放）
async fn free_port() -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    l.local_addr().unwrap().port()
}

// ============== 客户端启动 ==============

struct ClientHandle {
    client: Arc<TunnelClient>,
    task: JoinHandle<()>,
    connect_rx: mpsc::UnboundedReceiver<String>,
}

async fn spawn_client(server_url: &str, target_url: &str) -> ClientHandle {
    let client = Arc::new(TunnelClient::new(TunnelClientConfig {
        server_url: server_url.to_string(),
        token: "test-token".to_string(),
        target_url: target_url.to_string(),
        reconnect_interval: Duration::from_millis(50),
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
async fn auth_ok_ping_pong_and_events() {
    let mut server = FakeServer::start().await;
    let mut h = spawn_client(&server.ws_url(), "http://127.0.0.1:1").await;

    let mut conn = server.next_conn().await;
    let (token, force) = conn.expect_auth().await;
    assert_eq!(token, "test-token");
    assert!(!force);
    conn.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    conn.send(json!({"type": "ping"})).await;
    let msg = conn.next_message().await;
    assert_eq!(msg["type"], "pong", "应回 pong: {msg}");

    h.stop().await;
}

#[tokio::test]
async fn tcp_echo_bidirectional_with_sequence() {
    let mut server = FakeServer::start().await;
    let (port, stats) = start_echo_target(false).await;
    let mut h = spawn_client(&server.ws_url(), &format!("http://127.0.0.1:{port}")).await;

    let mut conn = server.next_conn().await;
    conn.expect_auth().await;
    conn.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    conn.send_tcp_connect("c1").await;
    while stats.conn_count() < 1 {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    conn.send_tcp_data("c1", b"hello-tcp", 0).await;
    let (payload, seq) = conn.expect_tcp_data("c1").await;
    assert_eq!(payload, b"hello-tcp");
    assert_eq!(seq, 0);

    conn.send_tcp_data("c1", "第二段".as_bytes(), 1).await;
    let (payload, seq) = conn.expect_tcp_data("c1").await;
    assert_eq!(payload, "第二段".as_bytes());
    assert_eq!(seq, 1);

    // 服务端主动关闭：客户端不回执 tcp_close
    conn.send(json!({"type": "tcp_close", "conn_id": "c1"})).await;
    let late = conn.try_next_message(Duration::from_millis(300)).await;
    assert!(late.is_none(), "服务端发起的 close 不应收到回执: {late:?}");

    h.stop().await;
}

#[tokio::test]
async fn tcp_target_refused_sends_close_with_error() {
    let mut server = FakeServer::start().await;
    let port = free_port().await;
    let mut h = spawn_client(&server.ws_url(), &format!("http://127.0.0.1:{port}")).await;

    let mut conn = server.next_conn().await;
    conn.expect_auth().await;
    conn.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    conn.send_tcp_connect("c-refused").await;
    let err = conn.expect_tcp_close("c-refused").await;
    assert!(err.unwrap().to_lowercase().contains("refused"));

    h.stop().await;
}

#[tokio::test]
async fn tcp_close_exactly_once_when_target_closes() {
    let mut server = FakeServer::start().await;
    let (port, stats) = start_echo_target(true).await; // 回写一次后关闭
    let mut h = spawn_client(&server.ws_url(), &format!("http://127.0.0.1:{port}")).await;

    let mut conn = server.next_conn().await;
    conn.expect_auth().await;
    conn.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    conn.send_tcp_connect("c-close").await;
    while stats.conn_count() < 1 {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    conn.send_tcp_data("c-close", b"x", 0).await;
    let (payload, _) = conn.expect_tcp_data("c-close").await;
    assert_eq!(payload, b"x");

    // 目标侧关闭 → error/close 都会触发，但只能收到一个 tcp_close
    let _ = conn.expect_tcp_close("c-close").await;
    let dup = conn.try_next_message(Duration::from_millis(300)).await;
    assert!(dup.is_none(), "tcp_close 必须幂等，只发一次: {dup:?}");

    h.stop().await;
}

#[tokio::test]
async fn ws_disconnect_cleans_tcp_and_reconnects() {
    let mut server = FakeServer::start().await;
    let (port, stats) = start_echo_target(false).await;
    let mut h = spawn_client(&server.ws_url(), &format!("http://127.0.0.1:{port}")).await;

    let mut conn1 = server.next_conn().await;
    conn1.expect_auth().await;
    conn1.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    conn1.send_tcp_connect("c-ws").await;
    while stats.conn_count() < 1 {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    // 服务端断开 WebSocket → 本地 TCP 连接被销毁（目标侧看到关闭），客户端随后重连
    conn1.sink.close().await.unwrap();
    drop(conn1);
    stats.wait_closed(1).await;

    let mut conn2 = server.next_conn().await;
    let (_, force) = conn2.expect_auth().await;
    assert!(!force, "非拒绝断开不应自动 force");
    conn2.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    // 重连后仍可正常转发
    conn2.send_tcp_connect("c-ws2").await;
    while stats.conn_count() < 2 {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    conn2.send_tcp_data("c-ws2", b"after-reconnect", 0).await;
    let (payload, _) = conn2.expect_tcp_data("c-ws2").await;
    assert_eq!(payload, b"after-reconnect");

    h.stop().await;
}

#[tokio::test]
async fn auth_rejected_then_reconnects_with_force() {
    // 与 TS 客户端一致：force 仅在「曾成功连接过之后再被拒」时自动携带
    let mut server = FakeServer::start().await;
    let mut h = spawn_client(&server.ws_url(), "http://127.0.0.1:1").await;

    // 1) 首次连接成功（was_connected = true）
    let mut conn1 = server.next_conn().await;
    let (_, f1) = conn1.expect_auth().await;
    assert!(!f1);
    conn1.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    // 2) 服务端断开（模拟网络抖动）
    conn1.sink.close().await.unwrap();
    drop(conn1);

    // 3) 重连被拒（模拟 token 被另一客户端占用）
    let mut conn2 = server.next_conn().await;
    let (_, f2) = conn2.expect_auth().await;
    assert!(!f2, "尚未被拒过，不带 force");
    conn2
        .send(json!({"type": "auth_error", "error": "already connected", "code": "auth_failed"}))
        .await;
    conn2.sink.close().await.unwrap();
    drop(conn2);

    // 4) 下一次认证应自动 force=true 抢占
    let mut conn3 = server.next_conn().await;
    let (_, f3) = conn3.expect_auth().await;
    assert!(f3, "曾连接过且被拒后，重连应自动 force");
    conn3.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    h.stop().await;
}

#[tokio::test]
async fn http_request_forwarded_to_target() {
    let mut server = FakeServer::start().await;
    let port = start_http_target("text").await;
    let mut h = spawn_client(&server.ws_url(), &format!("http://127.0.0.1:{port}")).await;

    let mut conn = server.next_conn().await;
    conn.expect_auth().await;
    conn.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    conn.send(json!({
        "type": "request", "id": "r1", "method": "GET", "path": "/x",
        "headers": {"X-Custom": "keep"}, "body": null, "timeout": 5
    }))
    .await;

    let msg = conn.next_message().await;
    assert_eq!(msg["type"], "response", "{msg}");
    assert_eq!(msg["id"], "r1");
    assert_eq!(msg["status"], 200);
    assert_eq!(msg["body"], "ok-from-target");
    // duration_ms 为 0 时按 wire 约定省略字段（serde skip_serializing_if）
    assert!(msg["duration_ms"].as_u64().map_or(true, |v| v < 60_000));

    h.stop().await;
}

#[tokio::test]
async fn sse_response_streamed() {
    let mut server = FakeServer::start().await;
    let port = start_http_target("sse").await;
    let mut h = spawn_client(&server.ws_url(), &format!("http://127.0.0.1:{port}")).await;

    let mut conn = server.next_conn().await;
    conn.expect_auth().await;
    conn.send_auth_ok("dsh").await;
    h.expect_connected("dsh").await;

    conn.send(json!({
        "type": "request", "id": "r2", "method": "GET", "path": "/sse",
        "headers": {}, "body": null, "timeout": 5
    }))
    .await;

    // stream_start
    let start = conn.next_message().await;
    assert_eq!(start["type"], "stream_start", "{start}");
    assert_eq!(start["status"], 200);

    // stream_chunk*（两段事件可能合并成一块，只要内容完整）
    let mut collected = String::new();
    let mut chunks = 0u32;
    loop {
        let msg = conn.next_message().await;
        match msg["type"].as_str().unwrap_or("") {
            "stream_chunk" => {
                collected.push_str(msg["data"].as_str().unwrap());
                chunks += 1;
            }
            "stream_end" => {
                assert!(collected.contains("data: msg-0"), "内容缺失: {collected}");
                assert!(collected.contains("data: msg-1"), "内容缺失: {collected}");
                assert!(chunks >= 1);
                assert!(msg["total_chunks"].as_u64().unwrap() >= 1);
                break;
            }
            other => panic!("意外的消息类型 {other}: {msg}"),
        }
    }

    h.stop().await;
}
