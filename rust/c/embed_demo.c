/*
 * 进程内嵌入演示：C 宿主自己应答隧道请求——**不开任何本地端口**。
 *
 * 构建与运行（macOS）：
 *   cd rust && cargo build --release
 *   cc c/embed_demo.c -I include -L target/release -ltunely -o /tmp/embed_demo
 *   DYLD_LIBRARY_PATH=target/release /tmp/embed_demo ws://127.0.0.1:8765/ws/tunnel tun_demo
 *
 * Linux 用 LD_LIBRARY_PATH；也可以直接链静态库（把 -ltunely 换成 target/release/libtunely.a）。
 *
 * 第三个参数是 target_url，默认故意指向 http://127.0.0.1:9（必然连不通），
 * 用来证明 handler 应答路径完全不依赖本地目标服务。
 */

#include <stdio.h>
#include <string.h>

#include "tunely.h"

static tunely_client_t *g_client = NULL;
static int g_handled = 0;

static void on_connect(const char *domain, void *user_data) {
    (void)user_data;
    printf("[c-host] connected: %s\n", domain ? domain : "(null)");
    fflush(stdout);
}

static void on_disconnect(void *user_data) {
    (void)user_data;
    printf("[c-host] disconnected\n");
    fflush(stdout);
}

static void on_error(const char *message, void *user_data) {
    (void)user_data;
    fprintf(stderr, "[c-host] error: %s\n", message ? message : "(null)");
}

static int handle_request(const tunely_request_t *request,
                          tunely_response_builder_t *response,
                          void *user_data) {
    (void)user_data;
    const char *method = tunely_request_method(request);
    const char *path = tunely_request_path(request);
    const char *body = tunely_request_body(request);

    printf("[c-host] request %s %s (headers=%zu, body=%s)\n",
           method ? method : "?", path ? path : "?",
           tunely_request_header_count(request), body ? body : "(none)");
    fflush(stdout);

    char payload[256];
    snprintf(payload, sizeof payload,
             "{\"from\":\"c-host\",\"method\":\"%s\",\"path\":\"%s\"}",
             method ? method : "", path ? path : "");

    tunely_response_set_status(response, 200);
    tunely_response_set_header(response, "content-type", "application/json");
    tunely_response_set_header(response, "x-tunely-embed", "c");
    tunely_response_set_body(response, payload, strlen(payload));

    /* 演示用：答完一个请求就收工（stop 可从回调线程调用） */
    if (++g_handled >= 1) {
        printf("[c-host] answered in-process, stopping\n");
        fflush(stdout);
        tunely_client_stop(g_client);
    }
    return 1; /* 1 = 已处理，不再访问 target_url */
}

int main(int argc, char **argv) {
    const char *server = argc > 1 ? argv[1] : "ws://127.0.0.1:8765/ws/tunnel";
    const char *token = argc > 2 ? argv[2] : "tun_demo";
    const char *target = argc > 3 ? argv[3] : "http://127.0.0.1:9";

    char config[1024];
    snprintf(config, sizeof config,
             "{\"server_url\":\"%s\",\"token\":\"%s\",\"target_url\":\"%s\"}",
             server, token, target);

    printf("[c-host] tunely ABI version %s\n", tunely_version());

    g_client = tunely_client_new(config);
    if (g_client == NULL) {
        fprintf(stderr, "[c-host] tunely_client_new failed: %s\n", tunely_last_error());
        return 1;
    }

    tunely_client_set_on_connect(g_client, on_connect, NULL);
    tunely_client_set_on_disconnect(g_client, on_disconnect, NULL);
    tunely_client_set_on_error(g_client, on_error, NULL);
    tunely_client_set_request_handler(g_client, handle_request, NULL);

    int rc = tunely_client_run(g_client);
    printf("[c-host] run returned %d (handled=%d)\n", rc, g_handled);

    tunely_client_free(g_client);
    return rc == TUNELY_OK ? 0 : 1;
}
