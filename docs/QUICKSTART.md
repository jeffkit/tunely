# WS-Tunnel 快速开始

本指南帮助你在 5 分钟内运行 WS-Tunnel。

## 前提条件

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（推荐，用于依赖管理与运行测试）
- Node.js 18+（如果使用 TypeScript 客户端）

## 步骤 1：安装服务端

```bash
cd python
uv sync                     # 安装运行依赖 + dev 组（pytest 等）
uv run pytest tests/ -q     # 可选：跑一遍测试确认环境正常
```

## 步骤 2：创建示例服务器

创建文件 `python/example_server.py`：

```python
import asyncio
from fastapi import FastAPI
from tunely import TunnelServer, TunnelServerConfig

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
    from tunely.repository import TunnelRepository
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
uv run python example_server.py
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

> 目标服务只需 fastapi + uvicorn，任意虚拟环境均可（`pip install fastapi uvicorn`）。

## 步骤 4：启动客户端

在新终端运行：

```bash
tunely connect \
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

## 受限网络：经 HTTP CONNECT 代理出站（Rust / TypeScript 客户端）

跨境直连受限（晚高峰吞吐骤降、UDP 受限）时，客户端 → server 的 WS 可经
HTTP CONNECT 代理转发。写进与 rust/python 同一份 `client.toml`：

```toml
# tunely-client.toml
server = "wss://your-server/ws/tunnel"
token = "tun_xxxxx"
target = "http://127.0.0.1:8080"
proxy = "http://proxy.lan:7890"   # 出站代理（客户端 → server 的 WS 经此转发）
```

```bash
tunely connect --config tunely-client.toml
```

也可用环境变量回退（无需改配置文件）：

```bash
export HTTPS_PROXY=http://proxy.lan:7890   # 亦识别 https_proxy / ALL_PROXY / all_proxy
tunely connect
```

要点：

- 优先级：**配置 > env > 无**；Rust ≥ 0.6.0 与 TypeScript ≥ 0.5.0 支持
  （Python 客户端暂不支持 `proxy`）。
- **作用域**：proxy 只作用于「客户端 → server」的 WS 出站；转发目标（target）
  的流量语义不变，不经代理。wss 场景 TLS 在 CONNECT 隧道内部完成，端到端加密不变。
- **v1 仅支持 HTTP CONNECT 代理，SOCKS 明确不支持**；对代理本身走 TLS 与
  代理认证暂不支持（配置了会启动即报错）。

## 故障排除

### 连接失败

1. 检查服务端是否运行在正确端口
2. 检查 Token 是否正确
3. 检查防火墙设置

### 转发超时

1. 检查目标服务是否运行
2. 检查目标服务端口是否正确
3. 增加超时时间
