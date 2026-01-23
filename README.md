# WS-Tunnel

WebSocket 透明反向代理隧道，支持服务端嵌入和客户端 SDK。

## 功能特性

- 🔌 **透明代理**：HTTP 请求通过 WebSocket 隧道转发，本地服务无感知
- 🏠 **服务端 SDK**：可嵌入到 FastAPI 应用中
- 🖥️ **客户端 SDK**：Python 和 TypeScript 双版本，支持独立运行和嵌入使用
- 🔐 **预注册机制**：域名 + Token 认证，安全可控
- 💾 **数据库支持**：SQLAlchemy 支持 SQLite / MySQL / PostgreSQL
- 📦 **数据迁移**：Alembic 管理数据库 Schema

## 快速开始

### 1. 安装服务端

```bash
cd packages/ws-tunnel/python
pip install -e .
```

### 2. 在 FastAPI 应用中使用

```python
from fastapi import FastAPI
from ws_tunnel import TunnelServer, TunnelServerConfig

app = FastAPI()

# 创建隧道服务器
config = TunnelServerConfig(
    database_url="sqlite+aiosqlite:///./data/tunnels.db"
)
tunnel_server = TunnelServer(config=config)

# 注册路由
app.include_router(tunnel_server.router)

@app.on_event("startup")
async def startup():
    await tunnel_server.initialize()

@app.on_event("shutdown")
async def shutdown():
    await tunnel_server.close()
```

### 3. 创建隧道

```bash
# 通过 API
curl -X POST http://localhost:8000/api/tunnels \
  -H "Content-Type: application/json" \
  -d '{"domain": "my-agent"}'

# 响应
{
  "domain": "my-agent",
  "token": "tun_xxxxxxxxxxxxx"
}
```

### 4. 启动客户端

**Python**：
```bash
ws-tunnel connect --token tun_xxxxx --target http://localhost:8080
```

**TypeScript**：
```bash
cd packages/ws-tunnel/typescript
pnpm install && pnpm build
node dist/cli.js connect --token tun_xxxxx --target http://localhost:8080
```

### 5. 转发请求

```bash
curl -X POST http://localhost:8000/api/tunnels/my-agent/forward \
  -H "Content-Type: application/json" \
  -d '{
    "method": "POST",
    "path": "/api/chat",
    "body": {"message": "hello"}
  }'
```

## 项目结构

```
packages/ws-tunnel/
├── README.md                  # 本文件
├── docs/
│   ├── PROTOCOL.md           # 协议文档
│   └── QUICKSTART.md         # 快速开始
│
├── python/                    # Python 实现
│   ├── pyproject.toml
│   ├── alembic/              # 数据库迁移
│   ├── ws_tunnel/
│   │   ├── __init__.py
│   │   ├── protocol.py       # 协议定义
│   │   ├── models.py         # 数据库模型
│   │   ├── database.py       # 数据库管理
│   │   ├── repository.py     # 数据仓库
│   │   ├── server.py         # 服务端 SDK
│   │   ├── client.py         # 客户端 SDK
│   │   ├── cli.py            # 命令行工具
│   │   └── config.py         # 配置
│   └── tests/                 # 测试
│
└── typescript/                # TypeScript 实现
    ├── package.json
    └── src/
        ├── protocol.ts       # 协议定义
        ├── client.ts         # 客户端 SDK
        └── cli.ts            # 命令行工具
```

## API 参考

### 服务端 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/ws/tunnel` | WebSocket | 客户端连接端点 |
| `/api/tunnels` | POST | 创建隧道 |
| `/api/tunnels` | GET | 列出所有隧道 |
| `/api/tunnels/{domain}` | GET | 获取隧道详情 |
| `/api/tunnels/{domain}` | DELETE | 删除隧道 |
| `/api/tunnels/{domain}/forward` | POST | 转发请求 |

### 配置选项

**服务端配置**（环境变量）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WS_TUNNEL_DATABASE_URL` | `sqlite+aiosqlite:///./data/tunnels.db` | 数据库连接 URL |
| `WS_TUNNEL_WS_PATH` | `/ws/tunnel` | WebSocket 端点路径 |
| `WS_TUNNEL_HEARTBEAT_INTERVAL` | `30` | 心跳间隔（秒） |
| `WS_TUNNEL_ADMIN_API_KEY` | - | 管理 API 密钥 |

**客户端配置**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--server` | `ws://localhost:8000/ws/tunnel` | 服务端 WebSocket URL |
| `--token` | (必填) | 隧道令牌 |
| `--target` | `http://localhost:8080` | 本地目标服务 URL |
| `--reconnect` | `5` | 重连间隔（秒） |

## 协议版本

当前协议版本：**1.0**

详见 [PROTOCOL.md](docs/PROTOCOL.md)

## License

MIT
