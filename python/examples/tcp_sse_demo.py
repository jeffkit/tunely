"""
TCP 隧道模式示例：SSE (Server-Sent Events)

演示如何通过 TCP 隧道转发 SSE 流式响应。
TCP 模式天然支持 SSE，因为它只是转发原始 TCP 字节流。
"""

import asyncio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from tunely import TunnelClient
import uvicorn


# ============== 本地 SSE 服务 ==============

app = FastAPI()


async def event_generator():
    """
    SSE 事件生成器
    
    生成实时事件流，模拟：
    - 实时日志
    - 进度更新
    - 聊天消息等场景
    """
    import time
    
    for i in range(10):
        # SSE 格式：data: <内容>\n\n
        timestamp = time.strftime("%H:%M:%S")
        yield f"data: {{\"id\": {i}, \"message\": \"Event {i}\", \"time\": \"{timestamp}\"}}\n\n"
        await asyncio.sleep(1)
    
    # 发送结束事件
    yield "data: {\"status\": \"completed\"}\n\n"


@app.get("/stream/events")
async def stream_events():
    """SSE 事件流端点"""
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


@app.get("/stream/chat")
async def stream_chat():
    """模拟 AI 聊天流式响应"""
    async def chat_stream():
        message = "这是一个通过 TCP 隧道传输的流式响应示例。"
        for char in message:
            yield f"data: {char}\n\n"
            await asyncio.sleep(0.1)
    
    return StreamingResponse(
        chat_stream(),
        media_type="text/event-stream"
    )


async def run_sse_service():
    """运行本地 SSE 服务"""
    config = uvicorn.Config(app, host="127.0.0.1", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


# ============== 隧道客户端 ==============

async def run_tunnel_client():
    """
    运行隧道客户端
    
    将本地 SSE 服务暴露到公网
    """
    client = TunnelClient(
        server_url="ws://localhost:8000/ws/tunnel",
        token="tun_your_token_here",  # 需要先创建 TCP 模式的隧道
        target_url="http://localhost:8080"
    )
    
    def on_connect():
        print("✅ TCP 隧道已连接")
        print(f"🌐 公网访问地址：https://{client.domain}.your-server.com")
    
    def on_disconnect():
        print("❌ TCP 隧道已断开，正在重连...")
    
    client.on_connect(on_connect)
    client.on_disconnect(on_disconnect)
    
    await client.run()


# ============== 请求方示例（消费 SSE） ==============

async def consume_sse_stream():
    """
    从公网消费 SSE 流
    
    这段代码可以在任何地方运行，只要能访问隧道服务器
    """
    import httpx
    
    print("\n" + "=" * 60)
    print("开始消费 SSE 流...")
    print("=" * 60)
    
    # 方式 1：使用 httpx 的流式接口
    async with httpx.AsyncClient(timeout=30.0) as client:
        async with client.stream(
            "GET",
            "https://my-tcp-tunnel.your-server.com/stream/events"
        ) as response:
            print(f"📡 连接状态: {response.status_code}")
            print(f"📋 响应头: {dict(response.headers)}\n")
            
            async for line in response.aiter_lines():
                if line:
                    print(f"📥 {line}")
    
    print("\n✅ SSE 流结束")


async def consume_sse_via_forward_api():
    """
    通过 forward API 消费 SSE 流
    
    这是使用隧道服务器的 forward API 的方式
    """
    import httpx
    
    print("\n" + "=" * 60)
    print("通过 Forward API 消费 SSE...")
    print("=" * 60)
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 注意：对于 TCP 模式，forward API 目前返回完整响应
        # 流式传输需要客户端特殊处理
        response = await client.post(
            "http://localhost:8000/api/tunnels/my-tcp-tunnel/forward",
            json={
                "method": "GET",
                "path": "/stream/events",
                "headers": {
                    "Accept": "text/event-stream"
                }
            }
        )
        
        print(f"📥 响应: {response.text}")


# ============== 主函数 ==============

async def main():
    print("=" * 60)
    print("TCP 隧道模式示例：SSE 流式响应")
    print("=" * 60)
    print()
    print("📖 说明：")
    print("   TCP 隧道天然支持 SSE，无需特殊处理")
    print("   只需将 SSE 数据作为普通 TCP 字节流转发")
    print()
    print("🚀 使用步骤：")
    print("   1. 创建 TCP 模式的隧道")
    print("   2. 启动本地 SSE 服务（端口 8080）")
    print("   3. 启动隧道客户端")
    print("   4. 从公网访问 SSE 流")
    print()
    
    # 运行本地服务
    print("✅ 示例代码已准备就绪！")
    print("\n💡 运行方式：")
    print("   python tcp_sse_demo.py")


if __name__ == "__main__":
    """
    完整运行流程：
    
    # 终端 1：启动本地 SSE 服务
    uvicorn tcp_sse_demo:app --port 8080
    
    # 终端 2：启动隧道客户端（连接到 TCP 模式的隧道）
    tunely client \\
      --server-url ws://your-server.com/ws/tunnel \\
      --token tun_xxxxxx \\
      --target-url http://localhost:8080
    
    # 终端 3：消费 SSE 流
    curl -N https://my-tcp-tunnel.your-server.com/stream/events
    
    # 或使用 Python 客户端
    # python -c "import asyncio; from tcp_sse_demo import consume_sse_stream; asyncio.run(consume_sse_stream())"
    """
    asyncio.run(main())
