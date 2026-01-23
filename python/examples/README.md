# tunely Showcase - 完整演示

这个示例演示 tunely 的所有功能，包括 SSE 流式响应。

## 目录结构

```
examples/
├── README.md           # 本文件
├── server.py           # 隧道服务端（嵌入 FastAPI）
├── client.py           # 隧道客户端（连接到服务端）
├── target_service.py   # 模拟目标服务（包含普通接口和 SSE 接口）
└── demo.py             # 一键演示脚本
```

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
