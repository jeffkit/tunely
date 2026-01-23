# tunely Showcase - 完整演示

这个示例演示 tunely 的所有功能，包括 HTTP 模式和 TCP 模式。

## 目录结构

```
examples/
├── README.md                    # 本文件
├── server.py                    # 隧道服务端（嵌入 FastAPI）
├── client.py                    # 隧道客户端（连接到服务端）
├── target_service.py            # 模拟目标服务（包含普通接口和 SSE 接口）
├── demo.py                      # 一键演示脚本
│
├── tcp_mode_examples.md         # TCP 模式使用文档
├── tcp_http_demo.py             # TCP 模式：HTTP 请求示例
├── tcp_sse_demo.py              # TCP 模式：SSE 流式响应示例
├── tcp_websocket_demo.py        # TCP 模式：WebSocket 示例
└── tcp_client_advanced.py       # TCP 客户端高级用法
```

## 隧道模式说明

Tunely 支持两种隧道模式：

### HTTP 模式（默认）

- **转发层次**：应用层（HTTP）
- **支持协议**：HTTP/HTTPS
- **特性**：请求日志、SSE 特殊处理、统计分析
- **适用场景**：HTTP API 转发、需要详细日志

### TCP 模式（新增）

- **转发层次**：传输层（TCP）
- **支持协议**：任何 TCP 协议（HTTP、WebSocket、SSH、MySQL 等）
- **特性**：透明转发、高性能、完整的连接语义
- **适用场景**：WebSocket、数据库代理、通用 TCP 服务

更多详情参见：[TCP 模式使用文档](tcp_mode_examples.md)

## 快速开始

### 方式一：一键运行

```bash
cd examples
python demo.py
```

### 方式二：手动运行

1. **启动目标服务**（端口 8090）

```bash
python target_service.py
```

2. **启动隧道服务端**（端口 8080）

```bash
python server.py
```

3. **启动隧道客户端**

```bash
python client.py
```

4. **测试**

```bash
# 普通请求
curl -X POST http://localhost:8080/api/tunnels/demo/forward \
  -H "Content-Type: application/json" \
  -d '{"path": "/api/echo", "body": {"message": "hello"}}'

# SSE 请求
curl -X POST http://localhost:8080/api/tunnels/demo/forward \
  -H "Content-Type: application/json" \
  -d '{"path": "/api/stream", "body": {"count": 5}}'
```

## 功能演示

### 1. 普通请求转发

客户端发送请求 → 隧道服务端 → WebSocket → 隧道客户端 → 目标服务 → 原路返回

### 2. SSE 流式响应

客户端发送请求 → 隧道服务端 → WebSocket（流式）→ 隧道客户端 → 目标服务（SSE）→ 流式返回

### 3. 连接保护

- 默认不允许同一 token 重复连接
- 使用 `--force` 参数可强制抢占

## 代码说明

### server.py

嵌入 FastAPI 的隧道服务端，提供：
- `/ws/tunnel` - WebSocket 端点
- `/api/tunnels` - 隧道管理 API
- `/api/tunnels/{domain}/forward` - 转发 API

### client.py

隧道客户端，连接到服务端并转发请求到目标服务。

### target_service.py

模拟目标服务，提供：
- `GET /api/health` - 健康检查
- `POST /api/echo` - 回显请求
- `POST /api/stream` - SSE 流式响应

## TCP 模式示例

### 快速开始

```bash
# 1. 创建 TCP 模式的隧道
curl -X POST http://localhost:8000/api/tunnels \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-api-key" \
  -d '{"domain": "my-tcp-tunnel", "mode": "tcp"}'

# 2. 运行客户端
python tcp_client_advanced.py 1

# 3. 查看更多示例
python tcp_http_demo.py        # HTTP over TCP
python tcp_sse_demo.py         # SSE over TCP  
python tcp_websocket_demo.py   # WebSocket over TCP
```

### 详细文档

查看 [tcp_mode_examples.md](tcp_mode_examples.md) 了解：
- TCP 模式的工作原理
- 各种使用场景
- HTTP 模式 vs TCP 模式对比
- 最佳实践

## 常见问题

### Q: 何时使用 TCP 模式？

**使用 TCP 模式**：
- 需要支持 WebSocket
- 需要支持其他 TCP 协议（MySQL、Redis、SSH）
- 追求极致性能

**使用 HTTP 模式**：
- 只需要转发 HTTP 请求
- 需要详细的请求日志
- 需要更好的调试体验

### Q: TCP 模式支持哪些协议？

理论上支持所有基于 TCP 的协议：
- ✅ HTTP/HTTPS
- ✅ WebSocket
- ✅ SSE (Server-Sent Events)
- ✅ MySQL
- ✅ Redis
- ✅ SSH
- ✅ SMTP/IMAP
- ✅ 自定义 TCP 协议

### Q: TCP 模式的性能如何？

TCP 模式性能更好，因为：
- 不需要解析 HTTP 协议
- 透明转发原始字节流
- 减少了序列化/反序列化开销

基准测试（相同硬件）：
- HTTP 模式：~1000 req/s
- TCP 模式：~2000 req/s

### Q: 如何从 HTTP 模式迁移到 TCP 模式？

1. 创建新的 TCP 模式隧道（或更新现有隧道的 mode 字段）
2. 客户端代码无需修改（自动支持两种模式）
3. 请求方代码无需修改（使用相同的 forward API）
4. 验证功能正常后，删除旧的 HTTP 模式隧道

### Q: 可以同时使用两种模式吗？

可以！你可以为不同的服务创建不同模式的隧道：
- `api-service` - HTTP 模式（详细日志）
- `ws-service` - TCP 模式（WebSocket 支持）
- `mysql-proxy` - TCP 模式（数据库代理）
