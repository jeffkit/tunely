# TCP 隧道模式使用示例

本文档展示如何使用 Tunely 的 TCP 隧道模式，支持任何基于 TCP 的协议。

## 概述

Tunely 支持两种隧道模式：

- **HTTP 模式**（默认）：在应用层转发 HTTP 请求，支持请求日志、SSE 流式响应等
- **TCP 模式**（新增）：在传输层转发原始 TCP 数据，支持任何 TCP 协议（HTTP、WebSocket、SSH、MySQL 等）

## 创建 TCP 模式的隧道

### 1. 通过 API 创建

```bash
curl -X POST http://localhost:8000/api/tunnels \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-api-key" \
  -d '{
    "domain": "my-tcp-tunnel",
    "name": "TCP Tunnel Demo",
    "description": "用于演示 TCP 模式",
    "mode": "tcp"
  }'
```

响应示例：

```json
{
  "domain": "my-tcp-tunnel",
  "token": "tun_xxxxxxxxxxxxxx",
  "name": "TCP Tunnel Demo",
  "mode": "tcp"
}
```

### 2. 启动客户端

```bash
tunely client \
  --server-url wss://your-server.com/ws/tunnel \
  --token tun_xxxxxxxxxxxxxx \
  --target-url http://localhost:8080
```

或使用 Python SDK：

```python
import asyncio
from tunely import TunnelClient

async def main():
    client = TunnelClient(
        server_url="wss://your-server.com/ws/tunnel",
        token="tun_xxxxxxxxxxxxxx",
        target_url="http://localhost:8080"  # 会自动解析 host 和 port
    )
    
    await client.run()

if __name__ == "__main__":
    asyncio.run(main())
```

## 使用场景示例

### 场景 1: HTTP 请求通过 TCP 隧道

TCP 隧道可以完全透明地转发 HTTP 请求，无需特殊处理。

**优点**：
- 支持所有 HTTP 特性（chunked encoding、connection keep-alive 等）
- 无需在隧道层解析 HTTP
- 性能更好

**示例**：

```python
# 请求方代码
import httpx

async def send_http_via_tcp_tunnel():
    """通过 TCP 隧道发送 HTTP 请求"""
    response = await httpx.post(
        "https://your-server.com/api/tunnels/my-tcp-tunnel/forward",
        json={
            "method": "POST",
            "path": "/api/chat",
            "headers": {
                "Content-Type": "application/json"
            },
            "body": {"message": "Hello"}
        }
    )
    return response.json()
```

### 场景 2: SSE (Server-Sent Events) 通过 TCP 隧道

TCP 隧道天然支持 SSE，因为它只是转发原始 TCP 字节流。

**客户端设置**：

```python
from tunely import TunnelClient

client = TunnelClient(
    server_url="wss://your-server.com/ws/tunnel",
    token="tun_xxxxxxxxxxxxxx",
    target_url="http://localhost:8080"  # 你的 SSE 服务
)

await client.run()
```

**本地 SSE 服务示例**：

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import asyncio

app = FastAPI()

async def event_generator():
    """SSE 事件生成器"""
    for i in range(10):
        yield f"data: Message {i}\n\n"
        await asyncio.sleep(1)

@app.get("/events")
async def sse_endpoint():
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )

# 运行：uvicorn sse_server:app --port 8080
```

**请求方代码**：

```python
import httpx

async def consume_sse_via_tcp_tunnel():
    """通过 TCP 隧道消费 SSE 流"""
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "GET",
            "https://your-server.com/api/tunnels/my-tcp-tunnel/forward",
            params={"path": "/events"}
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    print(line)
```

### 场景 3: WebSocket 通过 TCP 隧道

TCP 隧道完全支持 WebSocket 协议，因为 WebSocket 本质上是升级的 TCP 连接。

**本地 WebSocket 服务**：

```python
from fastapi import FastAPI, WebSocket
from fastapi.websockets import WebSocketDisconnect

app = FastAPI()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"Echo: {data}")
    except WebSocketDisconnect:
        print("Client disconnected")

# 运行：uvicorn ws_server:app --port 8080
```

**通过隧道连接 WebSocket**：

```python
import asyncio
import websockets

async def connect_ws_via_tcp_tunnel():
    """通过 TCP 隧道连接 WebSocket"""
    # 注意：需要通过隧道服务器的域名
    uri = "wss://my-tcp-tunnel.your-server.com/ws"
    
    async with websockets.connect(uri) as websocket:
        # 发送消息
        await websocket.send("Hello WebSocket!")
        
        # 接收响应
        response = await websocket.recv()
        print(f"收到: {response}")
```

### 场景 4: 其他 TCP 协议（MySQL、Redis、SSH 等）

TCP 隧道理论上支持任何 TCP 协议，但需要客户端支持协议转换。

**示例：MySQL 通过 TCP 隧道**

```python
# 注意：这需要客户端能够建立 MySQL 连接
# 当前实现需要进一步扩展以支持长连接协议

# 1. 客户端配置指向本地 MySQL
client = TunnelClient(
    server_url="wss://your-server.com/ws/tunnel",
    token="tun_xxxxxxxxxxxxxx",
    target_url="tcp://localhost:3306"  # MySQL 端口
)

# 2. 远程客户端通过隧道连接
import mysql.connector

conn = mysql.connector.connect(
    host="my-tcp-tunnel.your-server.com",
    port=3306,  # 隧道暴露的端口
    user="root",
    password="password",
    database="mydb"
)
```

## HTTP 模式 vs TCP 模式对比

| 特性 | HTTP 模式 | TCP 模式 |
|------|-----------|----------|
| **转发层次** | 应用层（HTTP） | 传输层（TCP） |
| **协议支持** | 仅 HTTP/HTTPS | 任何 TCP 协议 |
| **请求日志** | ✅ 详细日志 | ❌ 只记录连接 |
| **SSE 支持** | ✅ 特殊处理 | ✅ 自然支持 |
| **WebSocket** | ❌ 不支持 | ✅ 完全支持 |
| **性能** | 中等（需解析） | 高（透明转发） |
| **调试** | 容易 | 困难 |
| **适用场景** | HTTP API | 通用 TCP 服务 |

## 何时使用 TCP 模式

选择 TCP 模式的场景：

1. **需要支持 WebSocket**：HTTP 模式不支持 WebSocket 升级
2. **需要支持其他 TCP 协议**：如 MySQL、Redis、SSH 等
3. **追求极致性能**：避免 HTTP 解析开销
4. **需要完整的连接语义**：保持 TCP 连接的所有特性

继续使用 HTTP 模式的场景：

1. **只需要转发 HTTP 请求**：更简单、更好调试
2. **需要详细的请求日志**：HTTP 模式记录每个请求
3. **需要 SSE 流式响应的统计**：HTTP 模式可以统计数据块数量
4. **API 风格一致性**：HTTP 模式更符合 RESTful 风格

## 注意事项

### TCP 模式的当前限制

1. **单次请求-响应**：当前实现简化为单次请求-响应模式，不支持长连接复用
2. **无请求日志**：TCP 模式不记录详细的请求内容（因为不解析协议）
3. **调试困难**：无法直接查看传输的内容（都是二进制数据）

### 未来改进方向

1. **长连接支持**：支持 MySQL、Redis 等需要持久连接的协议
2. **连接池管理**：复用 TCP 连接，提高性能
3. **协议识别**：自动识别常见协议，提供更好的日志
4. **流量控制**：支持背压和流量限制

## 完整示例代码

查看 `examples/` 目录下的完整示例：

- `tcp_http_demo.py` - HTTP 通过 TCP 隧道
- `tcp_sse_demo.py` - SSE 通过 TCP 隧道
- `tcp_websocket_demo.py` - WebSocket 通过 TCP 隧道
- `tcp_client_advanced.py` - 高级客户端用法
