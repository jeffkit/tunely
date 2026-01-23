#!/usr/bin/env python
"""
隧道客户端示例

连接到隧道服务端，将请求转发到本地目标服务。
"""

import asyncio
import logging
import sys

from tunely import TunnelClient
from tunely.config import TunnelClientConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def get_demo_token() -> str:
    """从服务端获取演示隧道的 token"""
    import httpx
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("http://localhost:8080/api/tunnels/demo")
            if response.status_code == 200:
                data = response.json()
                # 注意：生产环境不应该这样获取 token，这里仅用于演示
                # 实际上需要从创建隧道时获取 token
                pass
            
            # 尝试创建隧道
            response = await client.post(
                "http://localhost:8080/api/tunnels",
                json={"domain": "demo", "name": "演示隧道"},
            )
            if response.status_code in (200, 201):
                data = response.json()
                return data["token"]
            elif response.status_code == 409:
                # 隧道已存在，从数据库获取 token（仅演示用）
                # 实际使用时，token 应该由创建者保存
                logger.warning("隧道已存在，请手动指定 token")
                return ""
    except Exception as e:
        logger.error(f"获取 token 失败: {e}")
    return ""


async def run_client(token: str, target_url: str = "http://localhost:8090", force: bool = False):
    """运行隧道客户端"""
    
    config = TunnelClientConfig(
        server_url="ws://localhost:8080/ws/tunnel",
        token=token,
        target_url=target_url,
        reconnect_interval=5.0,
        force=force,
    )
    
    client = TunnelClient(config=config)
    
    # 设置回调
    def on_connect():
        logger.info(f"✅ 已连接到隧道服务端，域名: {client.domain}")
    
    def on_disconnect():
        logger.info("⚠️ 连接断开")
    
    client.on_connect(on_connect)
    client.on_disconnect(on_disconnect)
    
    print("=" * 50)
    print("🔗 隧道客户端启动")
    print("=" * 50)
    print(f"服务端: {config.server_url}")
    print(f"目标服务: {config.target_url}")
    print(f"强制模式: {force}")
    print("=" * 50)
    
    try:
        await client.run()
    except KeyboardInterrupt:
        logger.info("客户端停止")
        await client.stop()


async def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="tunely 隧道客户端示例")
    parser.add_argument("--token", "-t", help="隧道 token")
    parser.add_argument("--target", "-T", default="http://localhost:8090", help="目标服务 URL")
    parser.add_argument("--force", "-f", action="store_true", help="强制抢占已有连接")
    args = parser.parse_args()
    
    token = args.token
    
    if not token:
        # 尝试从服务端获取演示 token
        print("未指定 token，尝试从服务端获取...")
        token = await get_demo_token()
        
        if not token:
            print()
            print("❌ 无法获取 token，请手动指定：")
            print()
            print("  1. 先创建隧道:")
            print('     curl -X POST http://localhost:8080/api/tunnels -H "Content-Type: application/json" -d \'{"domain": "demo"}\'')
            print()
            print("  2. 使用返回的 token 启动客户端:")
            print("     python client.py --token <token>")
            print()
            sys.exit(1)
        
        print(f"获取到 token: {token}")
    
    await run_client(token, args.target, args.force)


if __name__ == "__main__":
    asyncio.run(main())
