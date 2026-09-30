/*
 * tunely C ABI —— 让非 Rust 宿主进程内嵌入隧道客户端。
 *
 * 构建：
 *   cd rust && cargo build --release
 *   # 动态库：target/release/libtunely.dylib（macOS）/ libtunely.so（Linux）/ tunely.dll（Windows）
 *   # 静态库：target/release/libtunely.a
 *   cc my_host.c -I rust/include -L rust/target/release -ltunely -o my_host
 *
 * 内存与线程契约（重要）：
 *  1. 本 API 从不返回需要宿主 free 的指针。`tunely_last_error` / `tunely_version`
 *     与所有 `tunely_request_*` 返回的都是借用指针，只在文档写明的作用域内有效。
 *  2. 宿主通过 `tunely_response_set_*` 写回的字符串/字节会被库在回调返回前立刻
 *     拷贝，所以宿主可以用任何分配器（栈、GC 堆、arena），无需与 Rust 共享 free。
 *  3. 回调（on_connect / on_disconnect / on_error / request handler）由库的后台
 *     线程调用；`user_data` 的生命周期与并发安全由宿主负责，必须在
 *     `tunely_client_free` 之前一直有效。
 *  4. `tunely_request_handler_cb` 在库的 blocking 线程池上被调用，可以安全阻塞
 *     （读写文件、调用上游服务）；不要在里面调用 `tunely_client_run`。
 *  5. `tunely_client_run` 阻塞调用线程；`tunely_client_stop` 可从任意线程调用。
 *     句柄必须在 `tunely_client_run` 返回之后才能 `tunely_client_free`。
 *  6. 请求 handler 的返回值：1 = 已填好 response（不再访问 target_url）；
 *     0 = 未处理，回落 target_url 转发（与不注册 handler 的行为一致）。
 */

#ifndef TUNELY_H
#define TUNELY_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 返回码 */
#define TUNELY_OK 0
#define TUNELY_ERR_INVALID_ARG (-1)
#define TUNELY_ERR_CONFIG (-2)
#define TUNELY_ERR_RUNTIME (-3)
#define TUNELY_ERR_ALREADY_RUNNING (-4)

/* 不透明句柄 */
typedef struct tunely_client_t tunely_client_t;
typedef struct tunely_request_t tunely_request_t;
typedef struct tunely_response_builder_t tunely_response_builder_t;

/* 连接成功：domain 仅在回调期间有效 */
typedef void (*tunely_on_connect_cb)(const char *domain, void *user_data);
/* 连接断开 */
typedef void (*tunely_on_disconnect_cb)(void *user_data);
/* 错误：message 仅在回调期间有效 */
typedef void (*tunely_on_error_cb)(const char *message, void *user_data);

/*
 * 请求处理器。返回值 1 = 已用 response builder 写好响应；0 = 未处理（回落转发）。
 * request / response 指针仅在本次回调期间有效。
 */
typedef int (*tunely_request_handler_cb)(const tunely_request_t *request,
                                         tunely_response_builder_t *response,
                                         void *user_data);

/* ---------- 版本与错误 ---------- */

/* 客户端版本号（静态，勿 free） */
const char *tunely_version(void);

/* 最近一次错误（线程局部；下次本线程出错前有效，勿 free） */
const char *tunely_last_error(void);

/* ---------- 客户端生命周期 ---------- */

/*
 * 创建客户端。config_json 为 UTF-8 JSON：
 *   {"server_url":"wss://host/ws/tunnel","token":"tun_xxx",
 *    "target_url":"http://127.0.0.1:8080",
 *    "reconnect_interval":5,"max_reconnect_attempts":0,"request_timeout":300,
 *    "force":false,"keepalive_interval":25,"keepalive_timeout":45,"name":"embed"}
 * 前三个字段必填；其余可省略（取默认值：5s / 0=无限 / 300s / false / 25s / 45s）。
 * 失败返回 NULL，原因见 tunely_last_error()。
 */
tunely_client_t *tunely_client_new(const char *config_json);

/* 释放句柄（NULL 安全）。务必在 run 返回后调用。 */
void tunely_client_free(tunely_client_t *handle);

/* 注册事件回调；cb 传 NULL 视为参数错误（返回 TUNELY_ERR_INVALID_ARG） */
int tunely_client_set_on_connect(tunely_client_t *handle,
                                 tunely_on_connect_cb cb,
                                 void *user_data);
int tunely_client_set_on_disconnect(tunely_client_t *handle,
                                    tunely_on_disconnect_cb cb,
                                    void *user_data);
int tunely_client_set_on_error(tunely_client_t *handle,
                               tunely_on_error_cb cb,
                               void *user_data);

/*
 * 注册/卸载进程内请求处理器。cb = NULL 表示卸载，回到「全部转发 target_url」。
 * 需在 tunely_client_run 之前调用（handler 在建立连接时快照）。
 */
int tunely_client_set_request_handler(tunely_client_t *handle,
                                      tunely_request_handler_cb cb,
                                      void *user_data);

/* 阻塞运行直到 tunely_client_stop 或重连次数耗尽；不要在已有 tokio 运行时的线程调用 */
int tunely_client_run(tunely_client_t *handle);

/* 请求停止（任意线程可调） */
int tunely_client_stop(tunely_client_t *handle);

/* 是否处于运行态：1 = 是，0 = 否或参数非法 */
int tunely_client_is_running(tunely_client_t *handle);

/* ---------- 请求视图（只读，仅 handler 回调期间有效） ---------- */

const char *tunely_request_id(const tunely_request_t *request);
const char *tunely_request_method(const tunely_request_t *request);
const char *tunely_request_path(const tunely_request_t *request);
/* 无 body 时返回 NULL */
const char *tunely_request_body(const tunely_request_t *request);
size_t tunely_request_header_count(const tunely_request_t *request);
/* 越界返回 NULL */
const char *tunely_request_header_key(const tunely_request_t *request, size_t index);
const char *tunely_request_header_value(const tunely_request_t *request, size_t index);

/* ---------- 响应 builder（仅 handler 回调期间有效） ---------- */

/* 状态码，默认 200 */
int tunely_response_set_status(tunely_response_builder_t *response, uint16_t status);
/* 追加/覆盖响应头（key 已存在则覆盖） */
int tunely_response_set_header(tunely_response_builder_t *response,
                               const char *key,
                               const char *value);
/* 响应体：body = NULL 表示无 body；len 为字节数（按 UTF-8 lossy 拷贝） */
int tunely_response_set_body(tunely_response_builder_t *response,
                             const char *body,
                             size_t len);
/* 错误信息：写入 wire 协议的 error 字段；未显式设 status 时自动取 500 */
int tunely_response_set_error(tunely_response_builder_t *response,
                              const char *message);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* TUNELY_H */
