#!/usr/bin/env python
"""
tunely 功能演示

一键启动所有服务并演示：
1. 普通请求转发
2. SSE 流式响应转发
3. 连接保护机制
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx


async def wait_for_service(url: str, name: str, timeout: int = 30) -> bool:
    """等待服务启动"""
    print(f"⏳ 等待 {name} 启动...")
    start = time.time()
    async with httpx.AsyncClient() as client:
        while time.time() - start < timeout:
            try:
                response = await client.get(url, timeout=2)
                if response.status_code < 500:
                    print(f"✅ {name} 已就绪")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    print(f"❌ {name} 启动超时")
    return False


async def demo_normal_forward(token: str):
    """演示普通请求转发"""
    print()
    print("=" * 50)
    print("📤 演示 1: 普通请求转发")
    print("=" * 50)
    
    async with httpx.AsyncClient() as client:
        # 使用演示接口
        print("发送请求: POST /demo/forward")
        print('内容: {"path": "/api/echo", "body": {"message": "Hello from tunnel!"}}')
        print()
        
        response = await client.post(
            "http://localhost:8080/demo/forward",
            json={
                "path": "/api/echo",
                "body": {"message": "Hello from tunnel!", "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")},
            },
            timeout=30,
        )
        
        result = response.json()
        print("响应:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        if result.get("status") == 200:
            print()
            print("✅ 普通请求转发成功！")
        else:
            print()
            print(f"❌ 请求失败: {result.get('error')}")


async def demo_sse_forward(token: str):
    """演示 SSE 流式转发"""
    print()
    print("=" * 50)
    print("🌊 演示 2: SSE 流式响应转发")
    print("=" * 50)
    
    async with httpx.AsyncClient() as client:
        print("发送请求: POST /demo/stream")
        print('内容: {"path": "/api/stream", "body": {"count": 5, "delay": 0.3}}')
        print()
        print("流式响应:")
        print("-" * 40)
        
        async with client.stream(
            "POST",
            "http://localhost:8080/demo/stream",
            json={
                "path": "/api/stream",
                "body": {"count": 5, "delay": 0.3},
            },
            timeout=60,
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line[7:].strip()
                    print(f"[事件] {event}")
                elif line.startswith("data:"):
                    data = line[6:].strip()
                    try:
                        parsed = json.loads(data)
                        if "data" in parsed:
                            # 这是 chunk 数据，解析内部的 SSE 数据
                            inner_data = parsed["data"]
                            print(f"  数据: {inner_data}")
                        else:
                            print(f"  {json.dumps(parsed, ensure_ascii=False)}")
                    except Exception:
                        print(f"  {data}")
        
        print("-" * 40)
        print()
        print("✅ SSE 流式转发成功！")


async def demo_chat_stream(token: str):
    """演示聊天 SSE 流式转发"""
    print()
    print("=" * 50)
    print("💬 演示 3: 聊天 SSE 流式转发")
    print("=" * 50)
    
    async with httpx.AsyncClient() as client:
        print("发送请求: POST /demo/stream")
        print('内容: {"path": "/api/chat", "body": {"message": "你好，今天天气怎么样？"}}')
        print()
        print("流式响应:")
        print("-" * 40)
        
        output = ""
        async with client.stream(
            "POST",
            "http://localhost:8080/demo/stream",
            json={
                "path": "/api/chat",
                "body": {"message": "你好，今天天气怎么样？"},
            },
            timeout=60,
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data = line[6:].strip()
                    try:
                        parsed = json.loads(data)
                        if "data" in parsed:
                            inner_data = parsed["data"]
                            # 解析内部 SSE
                            if inner_data.startswith("data:"):
                                inner_json = inner_data[6:].strip()
                                inner_parsed = json.loads(inner_json)
                                if inner_parsed.get("event") == "token":
                                    char = inner_parsed.get("content", "")
                                    output += char
                                    print(char, end="", flush=True)
                                elif inner_parsed.get("event") == "thinking":
                                    print("🤔 思考中...", flush=True)
                                elif inner_parsed.get("event") == "done":
                                    print()  # 换行
                    except Exception:
                        pass
        
        print("-" * 40)
        print()
        print("✅ 聊天 SSE 流式转发成功！")


async def main():
    """主函数"""
    print()
    print("🎯 tunely 功能演示")
    print("=" * 50)
    print()
    
    # 检查是否已有服务在运行
    services_running = True
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            await client.get("http://localhost:8090/api/health")
            await client.get("http://localhost:8080/")
    except Exception:
        services_running = False
    
    processes = []
    script_dir = Path(__file__).parent
    
    if not services_running:
        print("启动服务...")
        print()
        
        # 启动目标服务
        target_proc = subprocess.Popen(
            [sys.executable, str(script_dir / "target_service.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append(target_proc)
        
        # 启动隧道服务端
        server_proc = subprocess.Popen(
            [sys.executable, str(script_dir / "server.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append(server_proc)
        
        # 等待服务启动
        if not await wait_for_service("http://localhost:8090/api/health", "目标服务"):
            print("❌ 服务启动失败")
            for p in processes:
                p.terminate()
            return
        
        if not await wait_for_service("http://localhost:8080/", "隧道服务端"):
            print("❌ 服务启动失败")
            for p in processes:
                p.terminate()
            return
    else:
        print("✅ 服务已在运行")
    
    # 获取或创建演示隧道
    token = None
    async with httpx.AsyncClient() as client:
        # 尝试创建隧道
        response = await client.post(
            "http://localhost:8080/api/tunnels",
            json={"domain": "demo", "name": "演示隧道"},
        )
        
        if response.status_code in (200, 201):
            token = response.json()["token"]
            print(f"✅ 创建演示隧道，token: {token}")
        elif response.status_code == 409:
            print("ℹ️ 演示隧道已存在，检查连接状态...")
            # 获取现有隧道列表
            response = await client.get("http://localhost:8080/api/tunnels")
            tunnels = response.json()
            demo_tunnel = next((t for t in tunnels if t["domain"] == "demo"), None)
            
            if demo_tunnel and demo_tunnel.get("connected"):
                print("✅ 隧道客户端已连接，直接使用现有隧道")
                token = None  # 不需要启动新客户端
            else:
                # 隧道存在但未连接，删除后重建
                print("🔄 隧道未连接，删除并重新创建...")
                delete_response = await client.delete("http://localhost:8080/api/tunnels/demo")
                if delete_response.status_code == 200:
                    print("✅ 已删除旧隧道")
                    # 重新创建
                    create_response = await client.post(
                        "http://localhost:8080/api/tunnels",
                        json={"domain": "demo", "name": "演示隧道"},
                    )
                    if create_response.status_code in (200, 201):
                        token = create_response.json()["token"]
                        print(f"✅ 重新创建演示隧道，token: {token}")
                    else:
                        print(f"❌ 重新创建隧道失败: {create_response.text}")
                        if processes:
                            for p in processes:
                                p.terminate()
                        return
                else:
                    print(f"❌ 删除隧道失败: {delete_response.text}")
                    if processes:
                        for p in processes:
                            p.terminate()
                    return
        else:
            print(f"❌ 创建隧道失败: {response.text}")
            if processes:
                for p in processes:
                    p.terminate()
            return
    
    # 如果有新 token，启动客户端
    client_proc = None
    if token:
        print()
        print("启动隧道客户端...")
        client_proc = subprocess.Popen(
            [sys.executable, str(script_dir / "client.py"), "--token", token],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append(client_proc)
        
        # 等待客户端连接
        await asyncio.sleep(2)
        
        # 检查是否连接成功
        async with httpx.AsyncClient() as client:
            response = await client.get("http://localhost:8080/api/tunnels")
            tunnels = response.json()
            demo_tunnel = next((t for t in tunnels if t["domain"] == "demo"), None)
            if not demo_tunnel or not demo_tunnel.get("connected"):
                print("❌ 隧道客户端连接失败")
                for p in processes:
                    p.terminate()
                return
        
        print("✅ 隧道客户端已连接")
    
    print()
    print("开始演示...")
    
    try:
        # 演示普通请求
        await demo_normal_forward(token)
        await asyncio.sleep(1)
        
        # 演示 SSE 流式
        await demo_sse_forward(token)
        await asyncio.sleep(1)
        
        # 演示聊天 SSE
        await demo_chat_stream(token)
        
        print()
        print("=" * 50)
        print("🎉 演示完成！")
        print("=" * 50)
        print()
        print("你可以继续测试:")
        print()
        print("  # 普通请求")
        print('  curl -X POST http://localhost:8080/demo/forward -H "Content-Type: application/json" -d \'{"path": "/api/echo", "body": {"message": "test"}}\'')
        print()
        print("  # SSE 流式")
        print('  curl -X POST http://localhost:8080/demo/stream -H "Content-Type: application/json" -d \'{"path": "/api/stream", "body": {"count": 3}}\'')
        print()
        
        if processes:
            input("按 Enter 键停止所有服务...")
    finally:
        if processes:
            print("停止服务...")
            for p in processes:
                p.terminate()
            print("✅ 服务已停止")


if __name__ == "__main__":
    asyncio.run(main())
