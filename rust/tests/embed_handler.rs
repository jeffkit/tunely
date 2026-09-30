//! 嵌入形态测试：进程内 handler（缓冲 / 流式 / 回落转发）+ C ABI（FFI）端到端。
//!
//! 全部用例都把 `target_url` 指向 `http://127.0.0.1:9`（必然拒连），用来证明
//! **handler 应答路径完全不依赖本地目标服务**——这正是「嵌入式客户端不必再
//! 起一个本地端口」的核心语义。

use std::collections::HashMap;
use std::ffi::{c_void, CStr, CString};
use std::os::raw::c_char;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpListener;
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message as WsMessage;

use tunely::client::{TunnelClient, TunnelClientConfig};
use tunely::handler::{
    sync_handler, HandlerOutcome, HandlerRequest, HandlerResponse, HandlerStream, RequestHandler,
};

const RECV_TIMEOUT: Duration = Duration::from_secs(5);
/// 必然拒连的目标：handler 命中时它不该被访问
const DEAD_TARGET: &str = "http://127.0.0.1:9";

// ============== 假隧道服务端 ==============

struct FakeServer {
    addr: std::net::SocketAddr,
    outgoing: mpsc::UnboundedSender<String>,
    incoming: mpsc::UnboundedReceiver<Value>,
}

impl FakeServer {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (out_tx, mut out_rx) = mpsc::unbounded_channel::<String>();
        let (in_tx, in_rx) = mpsc::unbounded_channel::<Value>();

        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accept 失败");
            let ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("WS 握手失败");
            let (mut sink, mut stream) = ws.split();

            // 读 auth，回 auth_ok（不带 capabilities → 全 JSON 路径）
            if let Some(Ok(WsMessage::Text(text))) = stream.next().await {
                let auth: Value = serde_json::from_str(&text).expect("auth 不是 JSON");
                assert_eq!(auth["type"], "auth");
            }
            sink.send(WsMessage::Text(
                json!({"type": "auth_ok", "domain": "embed.test", "tunnel_id": "t-embed"})
                    .to_string(),
            ))
            .await
            .unwrap();

            loop {
                tokio::select! {
                    queued = out_rx.recv() => match queued {
                        Some(text) => {
                            if sink.send(WsMessage::Text(text)).await.is_err() {
                                break;
                            }
                        }
                        None => break,
                    },
                    inbound = stream.next() => match inbound {
                        Some(Ok(WsMessage::Text(text))) => {
                            if let Ok(v) = serde_json::from_str::<Value>(&text) {
                                if in_tx.send(v).is_err() {
                                    break;
                                }
                            }
                        }
                        Some(Ok(_)) => {}
                        _ => break,
                    },
                }
            }
        });

        Self {
            addr,
            outgoing: out_tx,
            incoming: in_rx,
        }
    }

    fn endpoint(&self) -> String {
        format!("ws://{}/ws/tunnel", self.addr)
    }

    async fn send(&self, value: Value) {
        self.outgoing.send(value.to_string()).expect("服务端已退出");
    }

    async fn next(&mut self) -> Value {
        tokio::time::timeout(RECV_TIMEOUT, self.incoming.recv())
            .await
            .expect("等待客户端回包超时")
            .expect("客户端连接已关闭")
    }

    /// 断言「在超时窗口内客户端没有再发任何帧」（用于验证流结束语义）
    async fn expect_silence(&mut self) {
        let extra = tokio::time::timeout(Duration::from_millis(300), self.incoming.recv()).await;
        assert!(extra.is_err(), "不应再有回包，却收到: {:?}", extra);
    }
}

/// 起客户端并等它连上（用 on_connect 同步，避免 sleep 竞态）
async fn start_client<H: RequestHandler>(server: &FakeServer, handler: H) -> Arc<TunnelClient> {
    let config = TunnelClientConfig {
        server_url: server.endpoint(),
        token: "tok-embed".to_string(),
        target_url: DEAD_TARGET.to_string(),
        reconnect_interval: Duration::from_millis(50),
        ..Default::default()
    };
    let client = Arc::new(TunnelClient::new(config).with_request_handler(handler));
    assert!(client.has_request_handler());

    let (ready_tx, ready_rx) = oneshot::channel();
    let ready = Arc::new(Mutex::new(Some(ready_tx)));
    client.on_connect(move |_domain| {
        if let Some(tx) = ready.lock().unwrap().take() {
            let _ = tx.send(());
        }
    });

    let runner = client.clone();
    tokio::spawn(async move { runner.run().await });
    tokio::time::timeout(RECV_TIMEOUT, ready_rx)
        .await
        .expect("等待连接建立超时")
        .expect("on_connect 未触发");
    client
}

// ============== 1. 缓冲响应 ==============

#[tokio::test]
async fn handler_answers_in_process_without_local_target() {
    let mut server = FakeServer::start().await;
    let client = start_client(
        &server,
        sync_handler(|req: &HandlerRequest| {
            assert_eq!(req.id, "r-health");
            assert_eq!(req.method, "GET");
            assert_eq!(req.header("X-Demo"), Some("1")); // 大小写不敏感
            HandlerResponse::json(200, r#"{"ok":true}"#)
                .with_header("x-embed", "rust")
                .into()
        }),
    )
    .await;

    server
        .send(json!({
            "type": "request",
            "id": "r-health",
            "method": "GET",
            "path": "/embed/health",
            "headers": {"X-Demo": "1"},
        }))
        .await;

    let msg = server.next().await;
    assert_eq!(msg["type"], "response", "{msg}");
    assert_eq!(msg["id"], "r-health", "{msg}");
    assert_eq!(msg["status"], 200, "{msg}");
    assert_eq!(msg["headers"]["x-embed"], "rust", "{msg}");
    assert_eq!(
        msg["headers"]["content-type"], "application/json; charset=utf-8",
        "{msg}"
    );
    assert_eq!(msg["body"], r#"{"ok":true}"#, "{msg}");
    assert!(msg["error"].is_null(), "{msg}");

    client.stop();
}

// ============== 2. 回落转发 ==============

#[tokio::test]
async fn handler_forward_falls_back_to_target_url() {
    let mut server = FakeServer::start().await;
    let client = start_client(
        &server,
        sync_handler(|req: &HandlerRequest| {
            if req.path == "/handled" {
                HandlerResponse::text(200, "handled").into()
            } else {
                // 未处理 → 必须走 target_url（此处必然拒连 → 503）
                HandlerOutcome::Forward
            }
        }),
    )
    .await;

    server
        .send(json!({"type": "request", "id": "r-1", "method": "GET", "path": "/handled"}))
        .await;
    let handled = server.next().await;
    assert_eq!(handled["status"], 200, "{handled}");
    assert_eq!(handled["body"], "handled", "{handled}");

    server
        .send(json!({"type": "request", "id": "r-2", "method": "GET", "path": "/forwarded"}))
        .await;
    let forwarded = server.next().await;
    assert_eq!(forwarded["type"], "response", "{forwarded}");
    assert_eq!(forwarded["id"], "r-2", "{forwarded}");
    assert_eq!(
        forwarded["status"], 503,
        "回落转发应命中不可达 target 并回 503: {forwarded}"
    );
    assert!(
        forwarded["error"]
            .as_str()
            .unwrap_or_default()
            .contains("unavailable"),
        "{forwarded}"
    );

    client.stop();
}

// ============== 3. 流式响应 ==============

#[tokio::test]
async fn handler_stream_emits_stream_start_chunks_end() {
    let mut server = FakeServer::start().await;
    let client = start_client(
        &server,
        sync_handler(|_req: &HandlerRequest| {
            HandlerOutcome::Stream(HandlerStream::new(
                200,
                HashMap::from([("content-type".to_string(), "text/event-stream".to_string())]),
                futures_util::stream::iter(vec![
                    Ok("data: one\n\n".to_string()),
                    Ok("data: two\n\n".to_string()),
                ]),
            ))
        }),
    )
    .await;

    server
        .send(json!({"type": "request", "id": "r-stream", "method": "GET", "path": "/sse"}))
        .await;

    let start = server.next().await;
    assert_eq!(start["type"], "stream_start", "{start}");
    assert_eq!(start["id"], "r-stream", "{start}");
    assert_eq!(start["status"], 200, "{start}");
    assert_eq!(start["headers"]["content-type"], "text/event-stream", "{start}");

    let chunk0 = server.next().await;
    assert_eq!(chunk0["type"], "stream_chunk", "{chunk0}");
    assert_eq!(chunk0["data"], "data: one\n\n", "{chunk0}");
    // wire 最小化：sequence=0 被 skip_serializing_if 省略（与 SSE 回传一致）
    assert!(chunk0.get("sequence").is_none(), "{chunk0}");

    let chunk1 = server.next().await;
    assert_eq!(chunk1["data"], "data: two\n\n", "{chunk1}");
    assert_eq!(chunk1["sequence"], 1, "{chunk1}");

    let end = server.next().await;
    assert_eq!(end["type"], "stream_end", "{end}");
    assert_eq!(end["total_chunks"], 2, "{end}");
    assert!(end["error"].is_null(), "{end}");

    server.expect_silence().await;
    client.stop();
}

// ============== 4. 流式响应中途出错 ==============

#[tokio::test]
async fn handler_stream_error_lands_in_stream_end() {
    let mut server = FakeServer::start().await;
    let client = start_client(
        &server,
        sync_handler(|_req: &HandlerRequest| {
            HandlerOutcome::Stream(HandlerStream::new(
                200,
                HashMap::new(),
                futures_util::stream::iter(vec![
                    Ok("partial".to_string()),
                    Err("upstream blew up".to_string()),
                    Ok("never sent".to_string()),
                ]),
            ))
        }),
    )
    .await;

    server
        .send(json!({"type": "request", "id": "r-err", "method": "GET", "path": "/sse-err"}))
        .await;

    assert_eq!(server.next().await["type"], "stream_start");
    assert_eq!(server.next().await["data"], "partial");
    let end = server.next().await;
    assert_eq!(end["type"], "stream_end", "{end}");
    assert_eq!(end["total_chunks"], 1, "{end}");
    assert_eq!(end["error"], "upstream blew up", "{end}");

    // Err 之后的 chunk 不再发送
    server.expect_silence().await;
    client.stop();
}

// ============== 5. C ABI：宿主 handler 端到端 ==============

extern "C" fn ffi_handler(
    request: *const tunely::ffi::tunely_request_t,
    response: *mut tunely::ffi::tunely_response_builder_t,
    user_data: *mut c_void,
) -> i32 {
    // SAFETY: 契约保证 request / response 在回调期间有效
    let path = unsafe { CStr::from_ptr(tunely::ffi::tunely_request_path(request)) }
        .to_str()
        .expect("path 不是 UTF-8");
    let hits = unsafe { &mut *(user_data as *mut u32) };
    *hits += 1;

    if path != "/ffi/echo" {
        return 0; // 未处理 → 回落 target_url
    }

    let body = r#"{"from":"c-abi"}"#;
    // SAFETY: 契约保证 response 在回调期间有效；字符串均为 NUL 结尾字面量
    unsafe {
        assert_eq!(
            tunely::ffi::tunely_response_set_status(response, 201),
            tunely::ffi::TUNELY_OK
        );
        assert_eq!(
            tunely::ffi::tunely_response_set_header(
                response,
                c"content-type".as_ptr(),
                c"text/plain; charset=utf-8".as_ptr(),
            ),
            tunely::ffi::TUNELY_OK
        );
        assert_eq!(
            tunely::ffi::tunely_response_set_body(
                response,
                body.as_ptr() as *const c_char,
                body.len(),
            ),
            tunely::ffi::TUNELY_OK
        );
    }
    1 // 已处理
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn c_abi_request_handler_answers_in_process() {
    let mut server = FakeServer::start().await;
    let config = CString::new(
        json!({
            "server_url": server.endpoint(),
            "token": "tok-ffi",
            "target_url": DEAD_TARGET,
            "reconnect_interval": 0.05,
        })
        .to_string(),
    )
    .unwrap();

    // SAFETY: config 是 NUL 结尾 C 字符串；hits 活得比 client 久
    let handle = unsafe { tunely::ffi::tunely_client_new(config.as_ptr()) };
    assert!(!handle.is_null(), "tunely_client_new 失败");

    let mut hits: u32 = 0;
    // SAFETY: handle 来自上面的 new；user_data 指向本函数的 hits
    assert_eq!(
        unsafe {
            tunely::ffi::tunely_client_set_request_handler(
                handle,
                Some(ffi_handler),
                &mut hits as *mut u32 as *mut c_void,
            )
        },
        tunely::ffi::TUNELY_OK
    );

    // tunely_client_run 自建 tokio 运行时 → 必须在普通线程上跑
    let addr = handle as usize;
    let runner = std::thread::spawn(move || {
        // SAFETY: addr 是上面 new 出来的句柄；运行期间不释放
        unsafe { tunely::ffi::tunely_client_run(addr as *mut tunely::ffi::tunely_client_t) }
    });

    server
        .send(json!({"type": "request", "id": "r-ffi", "method": "POST", "path": "/ffi/echo"}))
        .await;

    let msg = server.next().await;
    assert_eq!(msg["type"], "response", "{msg}");
    assert_eq!(msg["id"], "r-ffi", "{msg}");
    assert_eq!(msg["status"], 201, "{msg}");
    assert_eq!(msg["body"], r#"{"from":"c-abi"}"#, "{msg}");
    assert_eq!(
        msg["headers"]["content-type"], "text/plain; charset=utf-8",
        "{msg}"
    );

    // 未处理的路径仍回落 target_url（证明 C ABI 的 0 返回值语义）
    server
        .send(json!({"type": "request", "id": "r-ffi-skip", "method": "GET", "path": "/other"}))
        .await;
    let fallback = server.next().await;
    assert_eq!(fallback["id"], "r-ffi-skip", "{fallback}");
    assert_eq!(fallback["status"], 503, "{fallback}");

    assert_eq!(hits, 2, "两次请求都应进入宿主 handler");

    // SAFETY: handle 有效；stop 可从任意线程调用
    assert_eq!(
        unsafe { tunely::ffi::tunely_client_stop(handle) },
        tunely::ffi::TUNELY_OK
    );
    assert_eq!(
        runner.join().expect("run 线程 panic"),
        tunely::ffi::TUNELY_OK
    );
    // SAFETY: run 已返回，可以释放
    unsafe { tunely::ffi::tunely_client_free(handle) };
}

#[test]
fn c_abi_rejects_bad_config() {
    let bad_json = CString::new("{not json").unwrap();
    // SAFETY: bad_json 是 NUL 结尾 C 字符串
    let handle = unsafe { tunely::ffi::tunely_client_new(bad_json.as_ptr()) };
    assert!(handle.is_null());
    // SAFETY: tunely_last_error 返回线程局部借用指针
    let err = unsafe { CStr::from_ptr(tunely::ffi::tunely_last_error()) }
        .to_str()
        .unwrap();
    assert!(err.contains("解析失败"), "{err}");

    // 缺必填字段 → serde 报 missing field
    let missing_field = CString::new(r#"{"server_url":"ws://x","token":"t"}"#).unwrap();
    // SAFETY: 同上
    assert!(unsafe { tunely::ffi::tunely_client_new(missing_field.as_ptr()) }.is_null());
    // SAFETY: 同上
    let err = unsafe { CStr::from_ptr(tunely::ffi::tunely_last_error()) }
        .to_str()
        .unwrap();
    assert!(err.contains("target_url"), "{err}");

    // 字段存在但为空 → 显式必填校验
    let empty_field =
        CString::new(r#"{"server_url":"","token":"t","target_url":"http://127.0.0.1:9"}"#)
            .unwrap();
    // SAFETY: 同上
    assert!(unsafe { tunely::ffi::tunely_client_new(empty_field.as_ptr()) }.is_null());
    // SAFETY: 同上
    let err = unsafe { CStr::from_ptr(tunely::ffi::tunely_last_error()) }
        .to_str()
        .unwrap();
    assert!(err.contains("必填"), "{err}");

    // NULL 入参不应崩
    // SAFETY: 故意传 NULL —— 契约要求 NULL 安全
    assert!(unsafe { tunely::ffi::tunely_client_new(std::ptr::null()) }.is_null());
    assert_eq!(
        unsafe { tunely::ffi::tunely_client_run(std::ptr::null_mut()) },
        tunely::ffi::TUNELY_ERR_INVALID_ARG
    );
    // SAFETY: NULL 安全
    unsafe { tunely::ffi::tunely_client_free(std::ptr::null_mut()) };

    // 版本号与包版本一致（C 宿主用它做兼容判断）
    // SAFETY: 静态字符串
    let version = unsafe { CStr::from_ptr(tunely::ffi::tunely_version()) }
        .to_str()
        .unwrap();
    assert_eq!(version, env!("CARGO_PKG_VERSION"));
}
