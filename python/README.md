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
