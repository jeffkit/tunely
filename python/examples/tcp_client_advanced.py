"""
TCP 隧道客户端高级用法

展示如何在代码中嵌入 TCP 隧道客户端，实现更复杂的场景：
1. 动态切换目标服务
2. 自定义连接回调
3. 错误处理和重试
4. 多隧道管理
"""

import asyncio
import logging
from tunely import TunnelClient
from tunely.config import TunnelClientConfig

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ManagedTunnelClient:
    """
    托管的隧道客户端
    
    提供更高级的功能：
    - 自动重连
    - 状态监控
    - 事件回调
    """
    
    def __init__(self, name: str, config: TunnelClientConfig):
        self.name = name
        self.config = config
        self.client = TunnelClient(config=config)
        self.is_ready = False
        self.connection_count = 0
        
        # 设置回调
        self.client.on_connect(self._on_connected)
        self.client.on_disconnect(self._on_disconnected)
    
    def _on_connected(self):
        """连接成功回调"""
        self.is_ready = True
        self.connection_count += 1
        logger.info(f"🟢 [{self.name}] 隧道已连接")
        logger.info(f"   域名: {self.client.domain}")
        logger.info(f"   连接次数: {self.connection_count}")
    
    def _on_disconnected(self):
        """断开连接回调"""
        self.is_ready = False
        logger.warning(f"🔴 [{self.name}] 隧道已断开")
    
    async def start(self):
        """启动隧道客户端"""
        logger.info(f"🚀 [{self.name}] 启动隧道客户端...")
        logger.info(f"   目标服务: {self.config.target_url}")
        await self.client.run()
    
    async def stop(self):
        """停止隧道客户端"""
        logger.info(f"🛑 [{self.name}] 停止隧道客户端...")
        await self.client.stop()


# ============== 场景 1：单个隧道管理 ==============

async def example_single_tunnel():
    """
    示例 1：单个隧道
    
    最简单的用法，运行单个 TCP 隧道
    """
    print("\n" + "=" * 60)
    print("示例 1：单个 TCP 隧道")
    print("=" * 60)
    
    config = TunnelClientConfig(
        server_url="ws://localhost:8000/ws/tunnel",
        token="tun_your_token_here",
        target_url="http://localhost:8080",
        reconnect_interval=5.0,  # 重连间隔
        max_reconnect_attempts=10,  # 最大重连次数
    )
    
    tunnel = ManagedTunnelClient("Main", config)
    
    try:
        await tunnel.start()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在停止...")
        await tunnel.stop()


# ============== 场景 2：多隧道管理 ==============

async def example_multiple_tunnels():
    """
    示例 2：管理多个隧道
    
    同时运行多个隧道客户端，连接到不同的本地服务
    """
    print("\n" + "=" * 60)
    print("示例 2：多个 TCP 隧道")
    print("=" * 60)
    
    # 定义多个隧道配置
    tunnels_config = [
        {
            "name": "API-Server",
            "token": "tun_api_token_xxx",
            "target_url": "http://localhost:8080",
        },
        {
            "name": "WebSocket-Server",
            "token": "tun_ws_token_xxx",
            "target_url": "http://localhost:8081",
        },
        {
            "name": "Database-Proxy",
            "token": "tun_db_token_xxx",
            "target_url": "tcp://localhost:3306",  # MySQL
        },
    ]
    
    # 创建隧道实例
    tunnels = []
    for cfg in tunnels_config:
        config = TunnelClientConfig(
            server_url="ws://localhost:8000/ws/tunnel",
            token=cfg["token"],
            target_url=cfg["target_url"],
        )
        tunnel = ManagedTunnelClient(cfg["name"], config)
        tunnels.append(tunnel)
    
    # 启动所有隧道
    tasks = [asyncio.create_task(t.start()) for t in tunnels]
    
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("收到中断信号，停止所有隧道...")
        for tunnel in tunnels:
            await tunnel.stop()


# ============== 场景 3：健康检查和监控 ==============

class MonitoredTunnelClient(ManagedTunnelClient):
    """
    带监控的隧道客户端
    
    增加健康检查和指标收集
    """
    
    def __init__(self, name: str, config: TunnelClientConfig):
        super().__init__(name, config)
        self.uptime = 0
        self.last_connected_at = None
        self._monitor_task = None
    
    def _on_connected(self):
        """连接成功回调（扩展）"""
        super()._on_connected()
        import time
        self.last_connected_at = time.time()
        
        # 启动监控任务
        if not self._monitor_task:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
    
    def _on_disconnected(self):
        """断开连接回调（扩展）"""
        super()._on_disconnected()
        
        # 停止监控任务
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
    
    async def _monitor_loop(self):
        """监控循环"""
        import time
        
        try:
            while self.is_ready:
                self.uptime = int(time.time() - self.last_connected_at)
                logger.info(f"📊 [{self.name}] 运行时长: {self.uptime}s, 连接次数: {self.connection_count}")
                await asyncio.sleep(30)  # 每 30 秒报告一次
        except asyncio.CancelledError:
            logger.info(f"🛑 [{self.name}] 监控停止")


async def example_monitored_tunnel():
    """
    示例 3：带监控的隧道
    
    演示如何监控隧道状态和收集指标
    """
    print("\n" + "=" * 60)
    print("示例 3：带监控的 TCP 隧道")
    print("=" * 60)
    
    config = TunnelClientConfig(
        server_url="ws://localhost:8000/ws/tunnel",
        token="tun_your_token_here",
        target_url="http://localhost:8080",
    )
    
    tunnel = MonitoredTunnelClient("Monitored", config)
    
    try:
        await tunnel.start()
    except KeyboardInterrupt:
        logger.info("停止中...")
        await tunnel.stop()


# ============== 场景 4：动态配置 ==============

async def example_dynamic_config():
    """
    示例 4：动态配置隧道
    
    根据运行时条件动态调整隧道配置
    """
    print("\n" + "=" * 60)
    print("示例 4：动态配置 TCP 隧道")
    print("=" * 60)
    
    # 从环境变量或配置文件读取
    import os
    
    server_url = os.getenv("TUNNEL_SERVER_URL", "ws://localhost:8000/ws/tunnel")
    token = os.getenv("TUNNEL_TOKEN", "tun_your_token_here")
    target_url = os.getenv("TARGET_URL", "http://localhost:8080")
    
    config = TunnelClientConfig(
        server_url=server_url,
        token=token,
        target_url=target_url,
    )
    
    logger.info(f"配置:")
    logger.info(f"  服务器: {config.server_url}")
    logger.info(f"  目标: {config.target_url}")
    
    client = TunnelClient(config=config)
    
    try:
        await client.run()
    except KeyboardInterrupt:
        logger.info("停止...")
        await client.stop()


# ============== 场景 5：错误处理和重试策略 ==============

async def example_error_handling():
    """
    示例 5：自定义错误处理
    
    演示如何处理连接错误和实现自定义重试策略
    """
    print("\n" + "=" * 60)
    print("示例 5：错误处理和重试")
    print("=" * 60)
    
    config = TunnelClientConfig(
        server_url="ws://localhost:8000/ws/tunnel",
        token="tun_your_token_here",
        target_url="http://localhost:8080",
        reconnect_interval=2.0,  # 快速重连
        max_reconnect_attempts=0,  # 无限重试
    )
    
    client = TunnelClient(config=config)
    
    def on_connect():
        logger.info("🎉 连接成功！")
    
    def on_disconnect():
        logger.warning("⚠️ 连接断开，将自动重连...")
    
    client.on_connect(on_connect)
    client.on_disconnect(on_disconnect)
    
    try:
        await client.run()
    except Exception as e:
        logger.error(f"❌ 致命错误: {e}")
        raise


# ============== 主函数 ==============

async def main():
    print("=" * 60)
    print("TCP 隧道客户端高级用法示例")
    print("=" * 60)
    print()
    print("📚 包含以下示例：")
    print("   1. 单个隧道管理")
    print("   2. 多个隧道管理")
    print("   3. 带监控的隧道")
    print("   4. 动态配置")
    print("   5. 错误处理和重试")
    print()
    print("💡 选择一个示例运行：")
    print("   python tcp_client_advanced.py 1")
    print("   python tcp_client_advanced.py 2")
    print("   python tcp_client_advanced.py 3")
    print("   python tcp_client_advanced.py 4")
    print("   python tcp_client_advanced.py 5")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        example_num = sys.argv[1]
        
        if example_num == "1":
            asyncio.run(example_single_tunnel())
        elif example_num == "2":
            asyncio.run(example_multiple_tunnels())
        elif example_num == "3":
            asyncio.run(example_monitored_tunnel())
        elif example_num == "4":
            asyncio.run(example_dynamic_config())
        elif example_num == "5":
            asyncio.run(example_error_handling())
        else:
            print(f"❌ 未知示例: {example_num}")
            asyncio.run(main())
    else:
        asyncio.run(main())
