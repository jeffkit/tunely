"""
TCP 隧道模式示例：HTTP 请求

演示如何通过 TCP 隧道转发 HTTP 请求。
TCP 模式会透明地转发所有 TCP 数据，包括 HTTP 协议。
"""

import asyncio
import httpx
from tunely import TunnelClient


# ============== 本地 HTTP 服务（目标服务）==============

from fastapi import FastAPI
import uvicorn

app = FastAPI()


@app.get("/")
async def root():
    return {"message": "Hello from local service!"}


@app.post("/api/echo")
async def echo(data: dict):
    """回显接口"""
    return {
        "echo": data,
        "received_at": "2026-01-23T22:00:00Z"
    }


@app.get("/api/health")
async def health():
    """健康检查"""
    return {"status": "healthy"}


async def run_local_service():
    """运行本地服务（端口 8080）"""
    config = uvicorn.Config(app, host="127.0.0.1", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


# ============== 隧道客户端 ==============

async def run_tcp_tunnel_client():
    """运行 TCP 隧道客户端"""
    client = TunnelClient(
        server_url="ws://localhost:8000/ws/tunnel",
        token="tun_your_token_here",  # 替换为实际的 token
        target_url="http://localhost:8080"
    )
    
    client.on_connect(lambda: print("✅ 隧道已连接"))
    client.on_disconnect(lambda: print("❌ 隧道已断开"))
    
    await client.run()


# ============== 请求方示例 ==============

async def send_http_request_via_tcp_tunnel():
    """
    通过 TCP 隧道发送 HTTP 请求
    
    注意：TCP 模式下，forward API 接受原始的 HTTP 数据
    """
    async with httpx.AsyncClient() as client:
        # 示例 1：GET 请求
        print("\n📤 发送 GET 请求...")
        response = await client.post(
            "http://localhost:8000/api/tunnels/my-tcp-tunnel/forward",
            json={
                "method": "GET",
                "path": "/api/health",
                "headers": {},
                "body": None
            }
        )
        print(f"📥 响应: {response.json()}")
        
        # 示例 2：POST 请求
        print("\n📤 发送 POST 请求...")
        response = await client.post(
            "http://localhost:8000/api/tunnels/my-tcp-tunnel/forward",
            json={
                "method": "POST",
                "path": "/api/echo",
                "headers": {
                    "Content-Type": "application/json"
                },
                "body": {"message": "Hello via TCP tunnel!"}
            }
        )
        print(f"📥 响应: {response.json()}")


# ============== 完整示例 ==============

async def main():
    """
    完整示例：启动本地服务、隧道客户端和发送请求
    
    实际使用时，这些通常运行在不同的进程中：
    1. 本地服务运行在内网
    2. 隧道客户端运行在内网（与本地服务同一网络）
    3. 请求方从公网访问
    """
    print("=" * 60)
    print("TCP 隧道模式示例：HTTP 请求")
    print("=" * 60)
    
    # 启动本地服务
    print("\n1️⃣ 启动本地 HTTP 服务 (端口 8080)...")
    service_task = asyncio.create_task(run_local_service())
    await asyncio.sleep(2)  # 等待服务启动
    
    # 启动隧道客户端（在实际使用中应该在单独的进程中运行）
    print("\n2️⃣ 启动隧道客户端...")
    # client_task = asyncio.create_task(run_tcp_tunnel_client())
    # await asyncio.sleep(2)  # 等待隧道连接
    
    # 发送请求（在实际使用中从公网发起）
    print("\n3️⃣ 通过隧道发送请求...")
    # await send_http_request_via_tcp_tunnel()
    
    print("\n✅ 示例完成！")
    print("\n💡 提示：在实际使用中，请按照以下步骤操作：")
    print("   1. 使用 API 创建 TCP 模式的隧道")
    print("   2. 在内网运行隧道客户端")
    print("   3. 从公网通过隧道访问内网服务")


if __name__ == "__main__":
    """
    运行方式：
    
    # 步骤 1：启动本地服务
    python tcp_http_demo.py
    
    # 步骤 2：在另一个终端启动隧道客户端
    tunely client --server-url ws://localhost:8000/ws/tunnel --token tun_xxx --target-url http://localhost:8080
    
    # 步骤 3：在第三个终端发送测试请求
    curl -X POST http://localhost:8000/api/tunnels/my-tcp-tunnel/forward \\
      -H "Content-Type: application/json" \\
      -d '{"method": "GET", "path": "/api/health"}'
    """
    asyncio.run(main())
