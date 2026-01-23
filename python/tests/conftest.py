"""
测试配置和 Fixtures
"""

import asyncio
import pytest
from typing import AsyncGenerator

from tunely.database import DatabaseManager
from tunely.models import Tunnel


@pytest.fixture(scope="session")
def event_loop():
    """创建事件循环"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db_manager() -> AsyncGenerator[DatabaseManager, None]:
    """创建测试数据库管理器"""
    # 使用内存数据库
    manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
async def sample_tunnel(db_manager: DatabaseManager) -> Tunnel:
    """创建示例隧道"""
    from tunely.repository import TunnelRepository

    async with db_manager.session() as session:
        repo = TunnelRepository(session)
        tunnel = await repo.create(
            domain="test-agent",
            token="test_token_12345",
            name="Test Tunnel",
            description="A test tunnel",
        )
        return tunnel
