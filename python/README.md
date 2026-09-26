# tunely - WebSocket 隧道

WebSocket 透明反向代理隧道 - Python 服务端和客户端 SDK。

## 特性

- **透明代理**: HTTP 请求通过 WebSocket 隧道转发
- **SSE 支持**: 完整支持 Server-Sent Events 流式响应 (v0.2.0+)
- **连接保护**: 防止意外抢占已有连接
- **自动重连**: 客户端断线自动重连

## 安装

```bash
pip install tunely
```

## 安装自检

uv 的缓存损坏可能导致**安装截断**——装出来的包文件不完整、部分路由缺失（uv 已知问题，参见 [astral-sh/uv#11043](https://github.com/astral-sh/uv/issues/11043)；hardlink 跨文件系统回退复制时也可能出问题）。因此装完必须自检 `server.py` 行数是否与仓库一致：

```bash
python - <<'EOF'
import pathlib, tunely
p = pathlib.Path(tunely.__file__).parent / "server.py"
print(p, sum(1 for _ in p.open()))
EOF
```

行数明显偏少即为截断，处理方式：

```bash
uv cache clean tunely
uv pip install --no-cache --reinstall-package tunely
```

部署环境也可在 service 中设置 `Environment=UV_LINK_MODE=copy` 规避 hardlink 问题。

## 使用

### 服务端（嵌入 FastAPI）

```python
from fastapi import FastAPI
from tunely import TunnelServer

app = FastAPI()
tunnel_server = TunnelServer()
app.include_router(tunnel_server.router)

@app.on_event("startup")
async def startup():
    await tunnel_server.initialize()

# 普通请求转发
response = await tunnel_server.forward(
    domain="my-agent",
    method="POST",
    path="/api/chat",
    body={"message": "hello"}
)

# SSE 流式转发 (v0.2.0+)
async for msg in tunnel_server.forward_stream(
    domain="my-agent",
    method="POST",
    path="/api/stream",
    body={"message": "hello"}
):
    if isinstance(msg, StreamStartMessage):
        print(f"Stream started: status={msg.status}")
    elif isinstance(msg, StreamChunkMessage):
        print(f"Chunk: {msg.data}")
    elif isinstance(msg, StreamEndMessage):
        print(f"Stream ended: {msg.total_chunks} chunks")
```

### 客户端

```python
from tunely import TunnelClient

client = TunnelClient(
    server_url="ws://server/ws/tunnel",
    token="tun_xxx",
    target_url="http://localhost:8080"
)
await client.run()
```

### 命令行

```bash
# 连接到隧道服务器
tunely connect --server ws://server/ws/tunnel --token tun_xxx --target http://localhost:8080

# 强制抢占已有连接
tunely connect --server ws://server/ws/tunnel --token tun_xxx --target http://localhost:8080 --force
```

## SSE 支持说明

从 v0.2.0 开始，tunely 自动检测 SSE 响应（Content-Type: text/event-stream）并进行流式传输：

1. **客户端**: 自动检测 SSE 响应，发送 StreamStart → StreamChunk* → StreamEnd 消息
2. **服务端**: 使用 `forward_stream()` 方法获取 AsyncIterator 处理流式数据

## 协议版本

- v1.0: 基础请求-响应
- v1.1: 添加 SSE 流式响应支持
