# 以库的形式嵌入 tunely 客户端

tunely 的客户端**不只是可执行程序**：三个语言实现都以「库 + CLI」双形态发布，
宿主进程可以把隧道直接跑在自己的进程里，不需要 spawn 子进程、不需要管状态文件。
Rust 版更进一步——它既能作为 crate 被 Rust 宿主嵌入，也能作为 C ABI 动态库
（`libtunely.dylib` / `libtunely.so`）被**任何**语言嵌入。

## 形态对照

| 语言 | 包形态 | 库入口 | 进程内 handler（零本地端口） | C ABI |
|---|---|---|---|---|
| Rust | crates.io `tunely`（`lib` + `cdylib` + `staticlib` + `bin`） | `tunely::client::TunnelClient` | ✅ `handler` 模块 | ✅ `include/tunely.h` |
| TypeScript | npm `tunely`（`main: dist/index.js` + `bin`） | `import { TunnelClient } from 'tunely'` | ❌ 仅事件通知 | — |
| Python | PyPI `tunely`（服务端 + 客户端同包） | `from tunely.client import TunnelClient` | ❌ 仅事件通知 | — |

三种形态的取舍：

1. **同语言进程内嵌入**——最省事，宿主与隧道共享事件循环/运行时，回调直接改宿主状态。
2. **C ABI（Rust 库）**——跨语言嵌入；宿主进程内拿到隧道能力，代价是要按 C 契约管理
   回调与 `user_data` 生命周期。
3. **子进程 sidecar**——崩溃隔离、零 FFI 风险，但要管进程、端口、状态文件与重启。

## 一、Rust 宿主：crate 嵌入

```toml
[dependencies]
tunely = "0.5"
tokio = { version = "1", features = ["rt-multi-thread", "macros"] }
```

### 1.1 传统形态：转发到本地端口

```rust
use tunely::client::{TunnelClient, TunnelClientConfig};

let client = TunnelClient::new(TunnelClientConfig {
    server_url: "wss://your-server/ws/tunnel".into(),
    token: "tun_xxxxx".into(),
    target_url: "http://127.0.0.1:3080".into(), // 宿主自己监听的端口
    ..Default::default()
});
client.on_connect(|domain| println!("已连接: {domain}"));
client.run().await; // 自动重连直到 client.stop()
```

`on_request` / `on_connect` / `on_disconnect` / `on_error` 都是**只读通知**：请求仍会转发到
`target_url`。宿主必须自己监听一个本地端口。

### 1.2 嵌入式形态：handler 直接代答（不用起端口）

装上 [`handler`](../rust/src/handler.rs) 后，服务端下发的每条 HTTP 请求先交给它：

- `HandlerOutcome::Forward` —— 不处理，**回落** `target_url`（与不装 handler 时逐字节一致）
- `HandlerOutcome::Response(..)` —— 缓冲响应，直接回给服务端
- `HandlerOutcome::Stream(..)` —— 流式响应，按 SSE 语义发 `stream_start/chunk*/stream_end`

```rust
use tunely::client::{TunnelClient, TunnelClientConfig};
use tunely::handler::{sync_handler, HandlerOutcome, HandlerResponse};

let client = TunnelClient::new(TunnelClientConfig {
    server_url: "wss://your-server/ws/tunnel".into(),
    token: "tun_xxxxx".into(),
    target_url: "http://127.0.0.1:9".into(), // 永不被命中，仅作回落兜底
    ..Default::default()
})
.with_request_handler(sync_handler(|req| {
    match req.path.as_str() {
        "/health" => HandlerResponse::json(200, r#"{"ok":true}"#).into(),
        // 其余请求照旧转发本地服务
        _ => HandlerOutcome::Forward,
    }
}));

client.run().await;
```

流式（LLM token 逐块回传、SSE 代理）：

```rust
use tunely::handler::{HandlerOutcome, HandlerStream};
use std::collections::HashMap;

HandlerOutcome::Stream(HandlerStream::new(
    200,
    HashMap::from([("content-type".into(), "text/event-stream".into())]),
    futures_util::stream::iter(vec![
        Ok("data: one\n\n".to_string()),
        Ok("data: two\n\n".to_string()),
        Err("upstream blew up".to_string()), // → 写进 stream_end.error 并终止
    ]),
))
```

需要 async 的 handler 用 `ClosureHandler`（闭包返回 `BoxFuture<HandlerOutcome>`）或自己
`impl RequestHandler`。

**作用域与线程语义**

- handler 只作用于 **HTTP 模式**；TCP/UDP 隧道是裸字节流（服务端直接与 `target_url`
  解析出的 `host:port` 建连），不经 handler。
- `handle` 在 tokio 工作线程上被 await；同步阻塞操作请自行 `tokio::task::spawn_blocking`。
- handler 在**连接建立时**快照：`run()` 之后再 `set_request_handler` 需要重连才生效。
- 装了 handler 的嵌入式客户端**不写状态文件**，`tunely status` 看不到它（状态文件逻辑
  在 `main.rs`，不在库里）。

## 二、任何语言：C ABI（cdylib / staticlib）

```bash
cd rust && cargo build --release
# macOS: target/release/libtunely.dylib    Linux: libtunely.so    Windows: tunely.dll
# 静态：target/release/libtunely.a

cc my_host.c -I rust/include -L rust/target/release -ltunely -o my_host
# 或直接链静态库：cc my_host.c -I rust/include rust/target/release/libtunely.a -o my_host
```

接口定义见 [`rust/include/tunely.h`](../rust/include/tunely.h)，
最小可运行宿主见 [`rust/c/embed_demo.c`](../rust/c/embed_demo.c)。

```c
#include "tunely.h"

static int handle(const tunely_request_t *req,
                  tunely_response_builder_t *resp,
                  void *user_data) {
    const char *path = tunely_request_path(req);
    if (strcmp(path, "/health") == 0) {
        tunely_response_set_status(resp, 200);
        tunely_response_set_header(resp, "content-type", "application/json");
        const char *body = "{\"ok\":true}";
        tunely_response_set_body(resp, body, strlen(body));
        return 1;              /* 1 = 已处理，不再访问 target_url */
    }
    return 0;                  /* 0 = 未处理，回落 target_url 转发 */
}

int main(void) {
    tunely_client_t *c = tunely_client_new(
        "{\"server_url\":\"wss://your-server/ws/tunnel\","
        "\"token\":\"tun_xxxxx\",\"target_url\":\"http://127.0.0.1:9\"}");
    if (!c) { fprintf(stderr, "%s\n", tunely_last_error()); return 1; }

    tunely_client_set_on_connect(c, on_connect, NULL);
    tunely_client_set_request_handler(c, handle, NULL);
    int rc = tunely_client_run(c);   /* 阻塞；tunely_client_stop(c) 可从任意线程打断 */
    tunely_client_free(c);
    return rc == TUNELY_OK ? 0 : 1;
}
```

**契约要点**

1. **内存归属单向**：库从不返回需要宿主 `free` 的指针。`tunely_last_error`、
   `tunely_version` 与所有 `tunely_request_*` 都是借用指针；宿主用
   `tunely_response_set_*` 写回的字节会被**立即拷贝**，宿主可以用栈/GC 堆/arena 任意分配器。
2. **回调线程**：`on_connect` / `on_disconnect` / `on_error` / request handler 都在库的
   后台线程上调用；`user_data` 的生命周期与并发安全由宿主负责，必须活到
   `tunely_client_free`。request handler 跑在 blocking 线程池上，可以安全阻塞。
3. **生命周期**：`tunely_client_run` 阻塞调用线程并自建 tokio 运行时（不要在已有 tokio
   运行时的线程里调）；`tunely_client_stop` 可从任意线程打断它；**必须等 `run` 返回后**
   才 `tunely_client_free`。
4. **响应是缓冲的**：C ABI 目前只暴露缓冲响应（`HandlerOutcome::Stream` 已存在于 Rust
   API，尚未映射到 C 回调形态）。
5. **错误**：`tunely_last_error()` 是**线程局部**的，下次本线程出错前有效。

### 端到端自检（仓库内）

```bash
cd rust
cargo build --release
cc -arch x86_64 c/embed_demo.c -I include -L target/release -ltunely -o /tmp/embed_demo   # 架构随本机
python3 c/fake_server.py 8795 &          # 单连接假服务端：下发一条 request，校验回包后退出
DYLD_LIBRARY_PATH=target/release /tmp/embed_demo ws://127.0.0.1:8795/ws/tunnel tun_demo
```

## 三、Python / TypeScript 现状

两个实现都已经可以直接在进程内跑隧道：

```python
from tunely.client import TunnelClient        # 客户端侧只需 httpx + websockets
task = asyncio.create_task(client.run())      # 与宿主 app 共用事件循环
```

```typescript
import { TunnelClient } from 'tunely';
await client.run();
```

- **Python：客户端不再拉服务端依赖**。`tunely/__init__.py` 用 PEP 562 惰性导入，
  `import tunely` / `import tunely.client` 不会加载 `fastapi` / `sqlalchemy`（这两个只在
  你访问 `tunely.TunnelServer` / `tunely.create_full_app` 时才导入）。
- **两者都还没有进程内 handler**：`on_request`（Python/TS）只是通知，宿主仍需监听本地
  端口。如需「零端口嵌入」，目前只有 Rust 版具备该能力。

## 四、还没有做的事（有意留白）

- Python / TypeScript 的 `on_request` 不支持返回响应（对齐 Rust `handler` 的语义）
- C ABI 未暴露流式响应（Rust API 已支持）
- TCP/UDP 隧道不经 handler（裸字节流语义，需要时另设 hook）
