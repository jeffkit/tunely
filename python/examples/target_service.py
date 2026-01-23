#!/usr/bin/env python
"""
模拟目标服务

提供：
- GET /api/health - 健康检查
- POST /api/echo - 回显请求
- POST /api/stream - SSE 流式响应
"""

import asyncio
import json
import logging
from datetime import datetime

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Target Service - 模拟目标服务")


class EchoRequest(BaseModel):
    """回显请求"""
    message: str
    timestamp: str | None = None


class StreamRequest(BaseModel):
    """流式请求"""
    count: int = 5
    delay: float = 0.5


@app.get("/api/health")
async def health():
    """健康检查"""
    return {"status": "ok", "service": "target_service", "timestamp": datetime.now().isoformat()}


@app.post("/api/echo")
async def echo(request: EchoRequest):
    """回显请求"""
    logger.info(f"收到回显请求: {request.message}")
    return {
        "echo": request.message,
        "timestamp": datetime.now().isoformat(),
        "original_timestamp": request.timestamp,
    }


@app.post("/api/stream")
async def stream(request: StreamRequest):
    """SSE 流式响应"""
    logger.info(f"收到流式请求: count={request.count}, delay={request.delay}")
    
    async def generate():
        for i in range(request.count):
            data = {
                "index": i,
                "message": f"这是第 {i + 1} 条消息",
                "timestamp": datetime.now().isoformat(),
            }
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            await asyncio.sleep(request.delay)
        
        # 发送结束事件
        yield f"data: {json.dumps({'event': 'done', 'total': request.count})}\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/chat")
async def chat(request: dict):
    """模拟聊天接口（SSE 流式）"""
    message = request.get("message", "")
    logger.info(f"收到聊天请求: {message}")
    
    async def generate():
        # 模拟思考
        yield f"data: {json.dumps({'event': 'thinking'})}\n\n"
        await asyncio.sleep(0.5)
        
        # 流式输出回复
        response = f"你好！你说的是：「{message}」。这是一个模拟的 AI 回复。"
        for i, char in enumerate(response):
            yield f"data: {json.dumps({'event': 'token', 'content': char, 'index': i})}\n\n"
            await asyncio.sleep(0.05)
        
        # 完成
        yield f"data: {json.dumps({'event': 'done', 'total_tokens': len(response)})}\n\n"
    
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
    print("🎯 目标服务启动")
    print("=" * 50)
    print("端口: 8090")
    print()
    print("可用接口:")
    print("  GET  /api/health   - 健康检查")
    print("  POST /api/echo     - 回显请求")
    print("  POST /api/stream   - SSE 流式响应")
    print("  POST /api/chat     - 模拟聊天 (SSE)")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8090)
