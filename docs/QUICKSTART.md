# WS-Tunnel 快速开始

本指南帮助你在 5 分钟内运行 WS-Tunnel。

## 前提条件

- Python 3.11+
- Node.js 18+（如果使用 TypeScript 客户端）

## 步骤 1：安装服务端

```bash
cd packages/ws-tunnel/python
pip install -e ".[dev]"
```

## 步骤 2：创建示例服务器

创建文件 `example_server.py`：

```python
import asyncio
from fastapi import FastAPI
from ws_tunnel import TunnelServer, TunnelServerConfig

app = FastAPI(title="WS-Tunnel Demo")

# 配置
config = TunnelServerConfig(
    database_url="sqlite+aiosqlite:///./demo_tunnels.db"
)
tunnel_server = TunnelServer(config=config)

# 注册路由
app.include_router(tunnel_server.router)

@app.on_event("startup")
async def startup():
    await tunnel_server.initialize()
    
    # 创建示例隧道
    from ws_tunnel.repository import TunnelRepository
    async with tunnel_server.db.session() as session:
        repo = TunnelRepository(session)
        existing = await repo.get_by_domain("demo-agent")
        if not existing:
            tunnel = await repo.create(
                domain="demo-agent",
                token="demo_token_12345",
                name="Demo Agent",
            )
            print(f"✓ 创建示例隧道: domain={tunnel.domain}, token={tunnel.token}")

@app.on_event("shutdown")
async def shutdown():
    await tunnel_server.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

运行服务器：

```bash
python example_server.py
```

## 步骤 3：创建目标服务

创建一个简单的目标服务（模拟 Agent）：

```python
# target_service.py
from fastapi import FastAPI

app = FastAPI(title="Target Service (Agent)")

@app.post("/api/chat")
async def chat(request: dict):
    message = request.get("message", "")
    return {"response": f"Echo: {message}"}

@app.get("/api/health")
async def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

在新终端运行：

```bash
python target_service.py
```

## 步骤 4：启动客户端

在新终端运行：

```bash
ws-tunnel connect \
  --server ws://localhost:8000/ws/tunnel \
  --token demo_token_12345 \
  --target http://localhost:8080
```

输出：
```
WS-Tunnel Client
  服务端: ws://localhost:8000/ws/tunnel
  目标: http://localhost:8080

✓ 已连接: domain=demo-agent
```

## 步骤 5：测试转发

发送测试请求：

```bash
curl -X POST http://localhost:8000/api/tunnels/demo-agent/forward \
  -H "Content-Type: application/json" \
  -d '{
    "method": "POST",
    "path": "/api/chat",
    "body": {"message": "Hello, World!"}
  }'
```

响应：
```json
{
  "status": 200,
  "headers": {"content-type": "application/json"},
  "body": {"response": "Echo: Hello, World!"},
  "duration_ms": 5
}
```

## 下一步

- 阅读 [README.md](../README.md) 了解完整功能
- 阅读 [PROTOCOL.md](PROTOCOL.md) 了解协议详情
- 查看 Python 和 TypeScript SDK 源码

## 故障排除

### 连接失败

1. 检查服务端是否运行在正确端口
2. 检查 Token 是否正确
3. 检查防火墙设置

### 转发超时

1. 检查目标服务是否运行
2. 检查目标服务端口是否正确
3. 增加超时时间
