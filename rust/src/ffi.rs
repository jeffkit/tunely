//! C ABI：把隧道客户端暴露给非 Rust 宿主（Go / C / C++ / C# / Java / Node-FFI …）
//!
//! 设计取舍（宿主只需读 `include/tunely.h`）：
//!
//! - **内存归属单向**：tunely 从不把「需要宿主释放」的指针交出去。回给宿主的
//!   `const char*`（请求字段、`tunely_last_error`）都是内部持有、生命周期在文档里
//!   写明的借用指针；宿主写回来的 body 由 tunely 在回调返回前**立即拷贝**，
//!   所以宿主可以用任何分配器（栈、GC 堆、arena），不需要和 Rust 共享 free。
//! - **回调可返回 NULL 语义**：所有回调 setter 接受 `NULL` 表示「清空」。
//! - **请求处理器是同步的**：宿主 handler 在 tokio 的 blocking 线程池上被调用，
//!   可以安全阻塞；返回 1 = 已用 builder 写好响应（不再访问 target_url），
//!   返回 0 = 未处理，回落 `target_url` 转发。
//! - **流式响应暂不经 C ABI 暴露**：Rust 侧 [`crate::handler::HandlerOutcome::Stream`]
//!   已可用，C ABI 先只给缓冲响应（chunk 级回调的形态留待有真实宿主需求时定）。
//!
//! 线程语义：`tunely_client_run` 阻塞**调用线程**并在其上建立 tokio 运行时；
//! 因此不得在已有 tokio 运行时的线程里调用（会 panic）。
//! `tunely_client_stop` 可从任意线程调用，用于让 `run` 返回。

#![allow(non_camel_case_types)]

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::{c_char, c_void, CStr, CString};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crate::client::{TunnelClient, TunnelClientConfig};
use crate::handler::{HandlerOutcome, HandlerRequest, HandlerResponse, RequestHandler};
use crate::protocol::CLIENT_VERSION;

// ============== 返回码 ==============

/// 成功
pub const TUNELY_OK: i32 = 0;
/// 入参非法（NULL、非 UTF-8、缺必填回调）
pub const TUNELY_ERR_INVALID_ARG: i32 = -1;
/// 配置解析失败（JSON 语法或必填字段缺失）
pub const TUNELY_ERR_CONFIG: i32 = -2;
/// 运行时创建/运行失败（详见 tunely_last_error）
pub const TUNELY_ERR_RUNTIME: i32 = -3;
/// 同一个 client 重复调用 run
pub const TUNELY_ERR_ALREADY_RUNNING: i32 = -4;

// ============== 错误信息（线程局部） ==============

thread_local! {
    static LAST_ERROR: RefCell<Option<CString>> = const { RefCell::new(None) };
}

fn set_last_error(msg: impl AsRef<str>) {
    let text = strip_nul(msg.as_ref());
    LAST_ERROR.with(|slot| {
        *slot.borrow_mut() = CString::new(text).ok();
    });
}

/// 最近一次错误信息（**线程局部**；下次本线程出错前有效，宿主不要释放）
#[no_mangle]
pub extern "C" fn tunely_last_error() -> *const c_char {
    LAST_ERROR.with(|slot| match slot.borrow().as_ref() {
        Some(s) => s.as_ptr(),
        None => std::ptr::null(),
    })
}

/// 客户端版本号（静态字符串，宿主不要释放）
#[no_mangle]
pub extern "C" fn tunely_version() -> *const c_char {
    // CLIENT_VERSION 来自 CARGO_PKG_VERSION，本身无 NUL，静态构造不会失败
    static VERSION: std::sync::OnceLock<CString> = std::sync::OnceLock::new();
    VERSION
        .get_or_init(|| CString::new(CLIENT_VERSION).unwrap_or_default())
        .as_ptr()
}

/// 去掉内嵌 NUL：C 字符串无法表达，宁可截断也不要让 CString::new 失败而丢信息
fn strip_nul(s: &str) -> String {
    s.replace('\0', "")
}

/// `*const c_char` → &str（NULL / 非 UTF-8 → None）
///
/// # Safety
/// 调用方保证 `ptr` 若非 NULL 则指向以 NUL 结尾的可读字符串。
unsafe fn cstr_to_str<'a>(ptr: *const c_char) -> Option<&'a str> {
    if ptr.is_null() {
        return None;
    }
    CStr::from_ptr(ptr).to_str().ok()
}

fn cstring_opt(ptr: *const c_char) -> Option<CString> {
    if ptr.is_null() {
        return None;
    }
    // SAFETY: 由调用方保证 ptr 合法（见各导出函数的前置条件）
    let s = unsafe { CStr::from_ptr(ptr) };
    Some(CString::new(s.to_bytes().to_vec()).unwrap_or_default())
}

// ============== 回调与 user_data ==============

/// 裸指针包装：FFI 契约下由宿主保证 user_data 的生命周期与线程安全，
/// Rust 侧需要显式声明 Send/Sync 才能塞进要求 Send + Sync 的回调里。
#[derive(Clone, Copy)]
struct UserData(*mut c_void);

impl UserData {
    /// 取裸指针。
    ///
    /// 刻意用方法而不是字段访问：Rust 2021 的闭包精确捕获会把 `ud.0` 拆成
    /// 「只捕获那个 `*mut c_void`」，而裸指针不是 Send/Sync，装不进要求
    /// `Send + Sync` 的事件回调；方法调用捕获的是整个 `UserData`（已声明
    /// Send/Sync），才能通过。
    fn as_ptr(self) -> *mut c_void {
        self.0
    }
}

// SAFETY: 见上——所有权与并发由宿主按 c 头文件契约负责
unsafe impl Send for UserData {}
// SAFETY: 同上
unsafe impl Sync for UserData {}

/// 连接成功回调：`domain` 仅回调期间有效
pub type tunely_on_connect_cb = extern "C" fn(domain: *const c_char, user_data: *mut c_void);
/// 连接断开回调
pub type tunely_on_disconnect_cb = extern "C" fn(user_data: *mut c_void);
/// 错误回调：`message` 仅回调期间有效
pub type tunely_on_error_cb = extern "C" fn(message: *const c_char, user_data: *mut c_void);

/// 请求处理器回调（在 tokio blocking 线程上调用，可阻塞）
///
/// 返回 1 = 已用 `response` builder 填好响应；返回 0 = 未处理，回落 target_url。
pub type tunely_request_handler_cb = extern "C" fn(
    request: *const tunely_request_t,
    response: *mut tunely_response_builder_t,
    user_data: *mut c_void,
) -> i32;

// ============== 请求视图（只读，仅回调期间有效） ==============

/// 服务端下发的请求视图；C 侧不透明，只能通过 `tunely_request_*` 读取
pub struct tunely_request_t {
    id: CString,
    method: CString,
    path: CString,
    body: Option<CString>,
    headers: Vec<(CString, CString)>,
}

impl From<HandlerRequest> for tunely_request_t {
    fn from(r: HandlerRequest) -> Self {
        let header = |s: &str| CString::new(strip_nul(s)).unwrap_or_default();
        Self {
            id: header(&r.id),
            method: header(&r.method),
            path: header(&r.path),
            body: r.body.as_deref().map(header),
            headers: r
                .headers
                .iter()
                .map(|(k, v)| (header(k), header(v)))
                .collect(),
        }
    }
}

/// 请求 id
/// # Safety
///
/// `req` 必须是指针由库传入宿主 handler 的那个请求视图（仅回调期间有效）。
#[no_mangle]
pub unsafe extern "C" fn tunely_request_id(req: *const tunely_request_t) -> *const c_char {
    // SAFETY: 空指针已判；非空时按契约指向本模块构造的 tunely_request_t
    unsafe { req.as_ref() }.map_or(std::ptr::null(), |r| r.id.as_ptr())
}

/// 请求方法（GET/POST/…）
/// # Safety
///
/// `req` 必须是指针由库传入宿主 handler 的那个请求视图（仅回调期间有效）。
#[no_mangle]
pub unsafe extern "C" fn tunely_request_method(req: *const tunely_request_t) -> *const c_char {
    // SAFETY: 同上
    unsafe { req.as_ref() }.map_or(std::ptr::null(), |r| r.method.as_ptr())
}

/// 请求路径（已归一化，恒以 `/` 开头）
/// # Safety
///
/// `req` 必须是指针由库传入宿主 handler 的那个请求视图（仅回调期间有效）。
#[no_mangle]
pub unsafe extern "C" fn tunely_request_path(req: *const tunely_request_t) -> *const c_char {
    // SAFETY: 同上
    unsafe { req.as_ref() }.map_or(std::ptr::null(), |r| r.path.as_ptr())
}

/// 请求体（无 body 时返回 NULL）
/// # Safety
///
/// `req` 必须是指针由库传入宿主 handler 的那个请求视图（仅回调期间有效）。
#[no_mangle]
pub unsafe extern "C" fn tunely_request_body(req: *const tunely_request_t) -> *const c_char {
    // SAFETY: 同上
    unsafe { req.as_ref() }
        .and_then(|r| r.body.as_ref())
        .map_or(std::ptr::null(), |b| b.as_ptr())
}

/// 请求头数量
/// # Safety
///
/// `req` 必须是指针由库传入宿主 handler 的那个请求视图（仅回调期间有效）。
#[no_mangle]
pub unsafe extern "C" fn tunely_request_header_count(req: *const tunely_request_t) -> usize {
    // SAFETY: 同上
    unsafe { req.as_ref() }.map_or(0, |r| r.headers.len())
}

/// 第 i 个请求头的 key（越界返回 NULL）
/// # Safety
///
/// `req` 必须是指针由库传入宿主 handler 的那个请求视图（仅回调期间有效）。
#[no_mangle]
pub unsafe extern "C" fn tunely_request_header_key(
    req: *const tunely_request_t,
    index: usize,
) -> *const c_char {
    // SAFETY: 同上
    unsafe { req.as_ref() }
        .and_then(|r| r.headers.get(index))
        .map_or(std::ptr::null(), |(k, _)| k.as_ptr())
}

/// 第 i 个请求头的 value（越界返回 NULL）
/// # Safety
///
/// `req` 必须是指针由库传入宿主 handler 的那个请求视图（仅回调期间有效）。
#[no_mangle]
pub unsafe extern "C" fn tunely_request_header_value(
    req: *const tunely_request_t,
    index: usize,
) -> *const c_char {
    // SAFETY: 同上
    unsafe { req.as_ref() }
        .and_then(|r| r.headers.get(index))
        .map_or(std::ptr::null(), |(_, v)| v.as_ptr())
}

// ============== 响应 builder（宿主在回调内填写） ==============

/// 响应填写器；C 侧不透明
pub struct tunely_response_builder_t {
    status: u16,
    headers: Vec<(String, String)>,
    body: Option<String>,
    error: Option<String>,
}

impl Default for tunely_response_builder_t {
    fn default() -> Self {
        Self {
            status: 200,
            headers: Vec::new(),
            body: None,
            error: None,
        }
    }
}

impl tunely_response_builder_t {
    fn into_outcome(self) -> HandlerOutcome {
        HandlerOutcome::Response(HandlerResponse {
            status: self.status,
            headers: self.headers.into_iter().collect::<HashMap<_, _>>(),
            body: self.body,
            error: self.error,
        })
    }
}

/// 设置状态码（默认 200）
/// # Safety
///
/// `resp` 必须是指针由库传入宿主 handler 的那个响应 builder（仅回调期间有效）。
#[no_mangle]
pub unsafe extern "C" fn tunely_response_set_status(
    resp: *mut tunely_response_builder_t,
    status: u16,
) -> i32 {
    match unsafe { resp.as_mut() } {
        Some(r) => {
            r.status = status;
            TUNELY_OK
        }
        None => TUNELY_ERR_INVALID_ARG,
    }
}

/// 追加/覆盖一个响应头
/// # Safety
///
/// `resp` 必须是指针由库传入宿主 handler 的那个响应 builder（仅回调期间有效）。
/// 传入的字符串必须是以 NUL 结尾的可读 C 字符串。
#[no_mangle]
pub unsafe extern "C" fn tunely_response_set_header(
    resp: *mut tunely_response_builder_t,
    key: *const c_char,
    value: *const c_char,
) -> i32 {
    let Some(r) = (unsafe { resp.as_mut() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let (Some(key), Some(value)) = (cstring_opt(key), cstring_opt(value)) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let key = key.to_string_lossy().to_string();
    let value = value.to_string_lossy().to_string();
    if let Some(existing) = r.headers.iter_mut().find(|(k, _)| *k == key) {
        existing.1 = value;
    } else {
        r.headers.push((key, value));
    }
    TUNELY_OK
}

/// 设置响应体（`body = NULL` 表示无 body；`len` 为字节数，允许非 UTF-8 负载，
/// 会按 UTF-8 lossy 拷贝——wire 协议的 body 字段是 JSON 字符串）
/// # Safety
///
/// `resp` 必须是指针由库传入宿主 handler 的那个响应 builder（仅回调期间有效）。
/// `body` 若非 NULL，必须至少有 `len` 字节可读。
#[no_mangle]
pub unsafe extern "C" fn tunely_response_set_body(
    resp: *mut tunely_response_builder_t,
    body: *const c_char,
    len: usize,
) -> i32 {
    let Some(r) = (unsafe { resp.as_mut() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    if body.is_null() {
        r.body = None;
        return TUNELY_OK;
    }
    // SAFETY: 调用方保证 body 至少有 len 字节可读
    let bytes = unsafe { std::slice::from_raw_parts(body as *const u8, len) };
    r.body = Some(String::from_utf8_lossy(bytes).to_string());
    TUNELY_OK
}

/// 设置错误信息（写进 wire 协议的 `error` 字段；status 未显式设置时默认 500）
/// # Safety
///
/// `resp` 必须是指针由库传入宿主 handler 的那个响应 builder（仅回调期间有效）。
/// 传入的字符串必须是以 NUL 结尾的可读 C 字符串。
#[no_mangle]
pub unsafe extern "C" fn tunely_response_set_error(
    resp: *mut tunely_response_builder_t,
    message: *const c_char,
) -> i32 {
    let Some(r) = (unsafe { resp.as_mut() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let Some(message) = cstring_opt(message) else {
        r.error = None;
        return TUNELY_OK;
    };
    r.error = Some(message.to_string_lossy().to_string());
    if r.status == 200 {
        r.status = 500;
    }
    TUNELY_OK
}

// ============== 客户端句柄 ==============

/// 隧道客户端句柄；C 侧不透明，由 `tunely_client_new` 创建
pub struct tunely_client_t {
    client: Arc<TunnelClient>,
    running: AtomicBool,
}

/// 配置 JSON（camelCase 或 snake_case 均可；`server_url`/`token`/`target_url` 必填）：
///
/// ```json
/// {"server_url":"wss://tunely.example.com/ws/tunnel","token":"tun_xxx",
///  "target_url":"http://127.0.0.1:8080","reconnect_interval":5,
///  "max_reconnect_attempts":0,"request_timeout":300,"force":false,
///  "keepalive_interval":25,"keepalive_timeout":45,"name":"my-embed"}
/// ```
#[derive(Debug, serde::Deserialize)]
struct FfiConfig {
    server_url: String,
    token: String,
    target_url: String,
    #[serde(default)]
    reconnect_interval: Option<f64>,
    #[serde(default)]
    max_reconnect_attempts: Option<u32>,
    #[serde(default)]
    request_timeout: Option<f64>,
    #[serde(default)]
    force: Option<bool>,
    #[serde(default)]
    keepalive_interval: Option<f64>,
    #[serde(default)]
    keepalive_timeout: Option<f64>,
    #[serde(default)]
    name: Option<String>,
}

impl FfiConfig {
    fn into_client_config(self) -> TunnelClientConfig {
        let default = TunnelClientConfig::default();
        TunnelClientConfig {
            server_url: self.server_url,
            token: self.token,
            target_url: self.target_url,
            reconnect_interval: self
                .reconnect_interval
                .map_or(default.reconnect_interval, secs),
            max_reconnect_attempts: self
                .max_reconnect_attempts
                .unwrap_or(default.max_reconnect_attempts),
            request_timeout: self.request_timeout.map_or(default.request_timeout, secs),
            force: self.force.unwrap_or(default.force),
            keepalive_interval: self
                .keepalive_interval
                .map_or(default.keepalive_interval, secs),
            keepalive_timeout: self
                .keepalive_timeout
                .map_or(default.keepalive_timeout, secs),
            name: self.name,
        }
    }
}

fn secs(v: f64) -> std::time::Duration {
    std::time::Duration::from_secs_f64(v.max(0.0))
}

/// 创建客户端（失败返回 NULL，原因见 `tunely_last_error`）。
///
/// 返回的句柄用完后必须 `tunely_client_free`；**不得**在 `tunely_client_run`
/// 尚未返回时释放。
/// # Safety
///
/// `config_json` 若非 NULL，必须是以 NUL 结尾的可读 C 字符串。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_new(config_json: *const c_char) -> *mut tunely_client_t {
    // SAFETY: 调用方保证 config_json 若非 NULL 则是合法 C 字符串
    let Some(text) = (unsafe { cstr_to_str(config_json) }) else {
        set_last_error("config_json 为 NULL 或不是合法 UTF-8");
        return std::ptr::null_mut();
    };
    let parsed: FfiConfig = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(e) => {
            set_last_error(format!("config JSON 解析失败: {e}"));
            return std::ptr::null_mut();
        }
    };
    if parsed.server_url.is_empty() || parsed.token.is_empty() || parsed.target_url.is_empty() {
        set_last_error("server_url / token / target_url 均为必填且不能为空");
        return std::ptr::null_mut();
    }
    Box::into_raw(Box::new(tunely_client_t {
        client: Arc::new(TunnelClient::new(parsed.into_client_config())),
        running: AtomicBool::new(false),
    }))
}

/// 释放客户端句柄（NULL 安全）。释放后该句柄不可再用。
/// # Safety
///
/// `handle` 必须是 [`tunely_client_new`] 返回且尚未释放的指针；
/// 调用时必须确保 `tunely_client_run` 已返回。NULL 安全。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_free(handle: *mut tunely_client_t) {
    if handle.is_null() {
        return;
    }
    // SAFETY: 句柄来自 tunely_client_new 的 Box::into_raw，且只释放一次
    drop(unsafe { Box::from_raw(handle) });
}

/// 注册连接成功回调（`cb = NULL` 清空）
/// # Safety
///
/// `handle` 必须来自 [`tunely_client_new`] 且尚未被 [`tunely_client_free`] 释放。
/// `user_data` 的生命周期与并发安全由宿主保证（回调期间可被后台线程使用）。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_set_on_connect(
    handle: *mut tunely_client_t,
    cb: Option<tunely_on_connect_cb>,
    user_data: *mut c_void,
) -> i32 {
    let Some(h) = (unsafe { handle.as_ref() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let Some(cb) = cb else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let ud = UserData(user_data);
    h.client.on_connect(move |domain| {
        if let Ok(c) = CString::new(strip_nul(domain)) {
            cb(c.as_ptr(), ud.as_ptr());
        }
    });
    TUNELY_OK
}

/// 注册断开回调（`cb = NULL` 清空）
/// # Safety
///
/// `handle` 必须来自 [`tunely_client_new`] 且尚未被 [`tunely_client_free`] 释放。
/// `user_data` 的生命周期与并发安全由宿主保证（回调期间可被后台线程使用）。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_set_on_disconnect(
    handle: *mut tunely_client_t,
    cb: Option<tunely_on_disconnect_cb>,
    user_data: *mut c_void,
) -> i32 {
    let Some(h) = (unsafe { handle.as_ref() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let Some(cb) = cb else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let ud = UserData(user_data);
    h.client.on_disconnect(move || cb(ud.as_ptr()));
    TUNELY_OK
}

/// 注册错误回调（`cb = NULL` 清空）
/// # Safety
///
/// `handle` 必须来自 [`tunely_client_new`] 且尚未被 [`tunely_client_free`] 释放。
/// `user_data` 的生命周期与并发安全由宿主保证（回调期间可被后台线程使用）。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_set_on_error(
    handle: *mut tunely_client_t,
    cb: Option<tunely_on_error_cb>,
    user_data: *mut c_void,
) -> i32 {
    let Some(h) = (unsafe { handle.as_ref() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let Some(cb) = cb else {
        return TUNELY_ERR_INVALID_ARG;
    };
    let ud = UserData(user_data);
    h.client.on_error(move |message| {
        if let Ok(c) = CString::new(strip_nul(message)) {
            cb(c.as_ptr(), ud.as_ptr());
        }
    });
    TUNELY_OK
}

/// 宿主请求处理器 → Rust handler 的桥
struct FfiHandler {
    cb: tunely_request_handler_cb,
    user_data: UserData,
}

/// 在 blocking 线程上调用宿主 handler。
///
/// 独立成函数（而不是把逻辑内联进闭包）是为了让 `spawn_blocking` 的闭包只捕获
/// 整体变量：Rust 2021 的精确捕获会拆到字段级，直接写 `user_data.0` 会把
/// `*mut c_void` 单独捕获进去，从而不满足 `Send`。
fn call_host_handler(
    cb: tunely_request_handler_cb,
    view: &tunely_request_t,
    user_data: UserData,
) -> HandlerOutcome {
    let mut builder = tunely_response_builder_t::default();
    let handled = cb(
        view as *const tunely_request_t,
        &mut builder as *mut tunely_response_builder_t,
        user_data.as_ptr(),
    );
    if handled == 1 {
        builder.into_outcome()
    } else {
        HandlerOutcome::Forward
    }
}

impl RequestHandler for FfiHandler {
    fn handle(
        &self,
        request: HandlerRequest,
    ) -> futures_util::future::BoxFuture<'static, HandlerOutcome> {
        let cb = self.cb;
        let user_data = self.user_data;
        let view = tunely_request_t::from(request);
        Box::pin(async move {
            // 宿主 handler 是同步 C 函数，可能阻塞（读文件、调上游…），
            // 放到 blocking 池执行，避免占住 tokio 工作线程。
            let outcome = tokio::task::spawn_blocking(move || call_host_handler(cb, &view, user_data))
                .await;
            // blocking 任务 panic/取消 → 按「未处理」回落，而不是让整条连接挂掉
            outcome.unwrap_or(HandlerOutcome::Forward)
        })
    }
}

/// 注册进程内请求处理器（`cb = NULL` 卸载，回到「全部转发 target_url」）
///
/// 必须在 `tunely_client_run` 之前调用（handler 在建立连接时快照）。
/// # Safety
///
/// `handle` 必须来自 [`tunely_client_new`] 且尚未被 [`tunely_client_free`] 释放。
/// `cb` 会在库的 blocking 线程池上被调用，`user_data` 的生命周期与并发安全由宿主保证。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_set_request_handler(
    handle: *mut tunely_client_t,
    cb: Option<tunely_request_handler_cb>,
    user_data: *mut c_void,
) -> i32 {
    let Some(h) = (unsafe { handle.as_ref() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    match cb {
        Some(cb) => {
            h.client
                .set_request_handler_arc(Arc::new(FfiHandler {
                    cb,
                    user_data: UserData(user_data),
                }));
            TUNELY_OK
        }
        None => {
            h.client.clear_request_handler();
            TUNELY_OK
        }
    }
}

/// 阻塞运行直到 `tunely_client_stop` 或重连耗尽；返回 0 表示干净退出。
///
/// 本函数在**调用线程**上建立 tokio 运行时，请不要从已有 tokio 运行时的线程调用。
/// # Safety
///
/// `handle` 必须来自 [`tunely_client_new`] 且尚未被 [`tunely_client_free`] 释放；
/// 释放前必须确保 `tunely_client_run` 已返回。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_run(handle: *mut tunely_client_t) -> i32 {
    let Some(h) = (unsafe { handle.as_ref() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    if h.running.swap(true, Ordering::SeqCst) {
        set_last_error("该 client 已在运行（tunely_client_run 只能调一次）");
        return TUNELY_ERR_ALREADY_RUNNING;
    }
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            set_last_error(format!("创建 tokio 运行时失败: {e}"));
            h.running.store(false, Ordering::SeqCst);
            return TUNELY_ERR_RUNTIME;
        }
    };
    runtime.block_on(h.client.run());
    h.running.store(false, Ordering::SeqCst);
    TUNELY_OK
}

/// 请求运行中的 client 停止（可从任意线程调用；未运行时为空操作）
/// # Safety
///
/// `handle` 必须来自 [`tunely_client_new`] 且尚未被 [`tunely_client_free`] 释放。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_stop(handle: *mut tunely_client_t) -> i32 {
    let Some(h) = (unsafe { handle.as_ref() }) else {
        return TUNELY_ERR_INVALID_ARG;
    };
    h.client.stop();
    TUNELY_OK
}

/// 是否已连接（1 = 已连接，0 = 未连接/参数非法）
/// # Safety
///
/// `handle` 必须来自 [`tunely_client_new`] 且尚未被 [`tunely_client_free`] 释放。
#[no_mangle]
pub unsafe extern "C" fn tunely_client_is_running(handle: *mut tunely_client_t) -> i32 {
    match unsafe { handle.as_ref() } {
        Some(h) => i32::from(h.running.load(Ordering::SeqCst)),
        None => 0,
    }
}
