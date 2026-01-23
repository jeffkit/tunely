#!/usr/bin/env python
"""
隧道服务端示例

将 TunnelServer 嵌入到 FastAPI 应用中。
"""

import asyncio
import json
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from tunely import TunnelServer, TunnelServerConfig, StreamStartMessage, StreamChunkMessage, StreamEndMessage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 创建 FastAPI 应用
app = FastAPI(title="Tunnel Server - 隧道服务端示例")

# 创建隧道服务器（使用 SQLite 存储隧道配置）
tunnel_server = TunnelServer(
    config=TunnelServerConfig(
        database_url="sqlite+aiosqlite:///./tunnels.db",
        ws_path="/ws/tunnel",
    )
)

# 注册隧道服务路由
app.include_router(tunnel_server.router)


@app.on_event("startup")
async def startup():
    """应用启动时初始化隧道服务器"""
    await tunnel_server.initialize()
    logger.info("隧道服务器已初始化")
    
    # 自动创建演示隧道
    try:
        from tunely.repository import TunnelRepository
        async with tunnel_server.db.session() as session:
            repo = TunnelRepository(session)
            existing = await repo.get_by_domain("demo")
            if not existing:
                tunnel = await repo.create(
                    domain="demo",
                    name="演示隧道",
                    description="用于演示的隧道",
                )
                logger.info(f"创建演示隧道: domain=demo, token={tunnel.token}")
            else:
                logger.info(f"演示隧道已存在: domain=demo, token={existing.token}")
    except Exception as e:
        logger.warning(f"创建演示隧道失败: {e}")


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "Tunnel Server",
        "version": "0.2.0",
        "endpoints": {
            "websocket": "/ws/tunnel",
            "create_tunnel": "POST /api/tunnels",
            "list_tunnels": "GET /api/tunnels",
            "forward": "POST /api/tunnels/{domain}/forward",
            "demo_forward": "POST /demo/forward",
            "demo_stream": "POST /demo/stream",
        },
    }


@app.post("/demo/forward")
async def demo_forward(request: dict):
    """
    演示普通请求转发
    
    请求示例:
    {
        "path": "/api/echo",
        "body": {"message": "hello"}
    }
    """
    domain = "demo"
    path = request.get("path", "/api/echo")
    body = request.get("body", {})
    
    response = await tunnel_server.forward(
        domain=domain,
        method="POST",
        path=path,
        body=body,
    )
    
    return {
        "status": response.status,
        "headers": response.headers,
        "body": response.body,
        "duration_ms": response.duration_ms,
        "error": response.error,
    }


@app.post("/demo/stream")
async def demo_stream(request: dict):
    """
    演示 SSE 流式转发
    
    请求示例:
    {
        "path": "/api/stream",
        "body": {"count": 5, "delay": 0.5}
    }
    """
    domain = "demo"
    path = request.get("path", "/api/stream")
    body = request.get("body", {"count": 5})
    
    async def generate():
        async for msg in tunnel_server.forward_stream(
            domain=domain,
            method="POST",
            path=path,
            body=body,
        ):
            if isinstance(msg, StreamStartMessage):
                yield f"event: start\ndata: {json.dumps({'status': msg.status, 'headers': msg.headers})}\n\n"
            elif isinstance(msg, StreamChunkMessage):
                yield f"event: chunk\ndata: {json.dumps({'sequence': msg.sequence, 'data': msg.data})}\n\n"
            elif isinstance(msg, StreamEndMessage):
                yield f"event: end\ndata: {json.dumps({'duration_ms': msg.duration_ms, 'total_chunks': msg.total_chunks, 'error': msg.error})}\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


if __name__ == "__main__":
    print("=" * 50)
    print("🚀 隧道服务端启动")
    print("=" * 50)
    print("端口: 8080")
    print()
    print("WebSocket 端点: ws://localhost:8080/ws/tunnel")
    print()
    print("演示接口:")
    print("  POST /demo/forward  - 普通请求转发")
    print("  POST /demo/stream   - SSE 流式转发")
    print()
    print("管理接口:")
    print("  POST /api/tunnels   - 创建隧道")
    print("  GET  /api/tunnels   - 列出隧道")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8080)
