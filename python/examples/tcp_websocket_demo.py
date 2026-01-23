"""
TCP 隧道模式示例：WebSocket

演示如何通过 TCP 隧道支持 WebSocket 连接。
这是 HTTP 模式无法实现的功能，因为 WebSocket 需要 TCP 连接升级。
"""

import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from tunely import TunnelClient
import uvicorn
import websockets


# ============== 本地 WebSocket 服务 ==============

app = FastAPI()


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket 聊天端点
    
    演示双向实时通信
    """
    await websocket.accept()
    print("✅ WebSocket 客户端已连接")
    
    try:
        while True:
            # 接收消息
            data = await websocket.receive_text()
            print(f"📥 收到: {data}")
            
            # 回显消息
            response = f"Echo: {data}"
            await websocket.send_text(response)
            print(f"📤 发送: {response}")
            
    except WebSocketDisconnect:
        print("❌ WebSocket 客户端已断开")


@app.websocket("/ws/realtime")
async def websocket_realtime(websocket: WebSocket):
    """
    WebSocket 实时数据推送
    
    演示服务端主动推送数据
    """
    await websocket.accept()
    print("✅ 实时推送客户端已连接")
    
    try:
        # 服务端主动推送数据
        for i in range(10):
            message = f"实时数据 #{i}"
            await websocket.send_text(message)
            print(f"📤 推送: {message}")
            await asyncio.sleep(1)
        
        await websocket.send_text("数据推送完成")
        
    except WebSocketDisconnect:
        print("❌ 客户端提前断开")


async def run_websocket_service():
    """运行本地 WebSocket 服务"""
    config = uvicorn.Config(app, host="127.0.0.1", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


# ============== 隧道客户端 ==============

async def run_tunnel_client():
    """
    运行 TCP 隧道客户端
    
    注意：必须使用 TCP 模式的隧道才能支持 WebSocket
    """
    client = TunnelClient(
        server_url="ws://localhost:8000/ws/tunnel",
        token="tun_your_tcp_token_here",  # 必须是 TCP 模式的隧道！
        target_url="http://localhost:8080"
    )
    
    def on_connect():
        print("✅ TCP 隧道已连接")
        print("🌐 WebSocket 访问地址：")
        print(f"   wss://{client.domain}.your-server.com/ws/chat")
        print(f"   wss://{client.domain}.your-server.com/ws/realtime")
    
    client.on_connect(on_connect)
    client.on_disconnect(lambda: print("❌ 隧道断开"))
    
    await client.run()


# ============== WebSocket 客户端示例 ==============

async def websocket_client_chat():
    """
    WebSocket 客户端示例：聊天
    
    从公网通过 TCP 隧道连接到内网的 WebSocket 服务
    """
    uri = "wss://my-tcp-tunnel.your-server.com/ws/chat"
    
    print(f"\n📡 连接到 WebSocket: {uri}")
    
    async with websockets.connect(uri) as websocket:
        print("✅ WebSocket 已连接")
        
        # 发送消息
        messages = ["Hello", "How are you?", "Goodbye"]
        
        for msg in messages:
            print(f"\n📤 发送: {msg}")
            await websocket.send(msg)
            
            # 接收回显
            response = await websocket.recv()
            print(f"📥 收到: {response}")
            
            await asyncio.sleep(1)
        
        print("\n✅ 对话完成")


async def websocket_client_realtime():
    """
    WebSocket 客户端示例：实时推送
    
    接收服务端主动推送的实时数据
    """
    uri = "wss://my-tcp-tunnel.your-server.com/ws/realtime"
    
    print(f"\n📡 连接到实时推送: {uri}")
    
    async with websockets.connect(uri) as websocket:
        print("✅ 已连接，等待数据推送...\n")
        
        try:
            while True:
                message = await websocket.recv()
                print(f"📥 收到推送: {message}")
                
                if "完成" in message:
                    break
        except websockets.exceptions.ConnectionClosed:
            print("\n❌ 连接已关闭")
        
        print("\n✅ 推送完成")


# ============== 主函数 ==============

async def main():
    print("=" * 60)
    print("TCP 隧道模式示例：WebSocket")
    print("=" * 60)
    print()
    print("📖 为什么需要 TCP 模式？")
    print("   • HTTP 模式只支持 HTTP 请求-响应")
    print("   • WebSocket 需要 TCP 连接升级")
    print("   • TCP 模式可以透明转发 WebSocket")
    print()
    print("🚀 使用步骤：")
    print("   1. 创建 TCP 模式的隧道（mode='tcp'）")
    print("   2. 启动本地 WebSocket 服务")
    print("   3. 启动隧道客户端（TCP 模式）")
    print("   4. 从公网通过隧道连接 WebSocket")
    print()
    print("✅ 示例代码已准备就绪！")


if __name__ == "__main__":
    """
    完整运行流程：
    
    ==================== 准备工作 ====================
    
    # 1. 创建 TCP 模式的隧道
    curl -X POST http://your-server.com/api/tunnels \\
      -H "Content-Type: application/json" \\
      -H "x-api-key: your-api-key" \\
      -d '{
        "domain": "my-ws-tunnel",
        "name": "WebSocket Tunnel",
        "mode": "tcp"
      }'
    
    # 记住返回的 token
    
    ==================== 运行服务 ====================
    
    # 终端 1：启动本地 WebSocket 服务
    uvicorn tcp_websocket_demo:app --port 8080
    
    # 终端 2：启动隧道客户端
    tunely client \\
      --server-url wss://your-server.com/ws/tunnel \\
      --token tun_xxxxxx \\
      --target-url http://localhost:8080
    
    ==================== 测试连接 ====================
    
    # 终端 3：使用命令行工具测试
    # 安装 wscat: npm install -g wscat
    wscat -c wss://my-ws-tunnel.your-server.com/ws/chat
    
    # 或使用 Python 客户端
    python -c "import asyncio; from tcp_websocket_demo import websocket_client_chat; asyncio.run(websocket_client_chat())"
    
    # 或测试实时推送
    python -c "import asyncio; from tcp_websocket_demo import websocket_client_realtime; asyncio.run(websocket_client_realtime())"
    
    ==================== 浏览器测试 ====================
    
    在浏览器控制台运行：
    
    const ws = new WebSocket('wss://my-ws-tunnel.your-server.com/ws/chat');
    
    ws.onopen = () => {
      console.log('✅ WebSocket 已连接');
      ws.send('Hello from browser!');
    };
    
    ws.onmessage = (event) => {
      console.log('📥 收到:', event.data);
    };
    
    ws.onerror = (error) => {
      console.error('❌ 错误:', error);
    };
    """
    asyncio.run(main())
