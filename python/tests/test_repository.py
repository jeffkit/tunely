"""
数据仓库测试
"""

import pytest
from tunely.database import DatabaseManager
from tunely.repository import TunnelRepository


class TestTunnelRepository:
    """测试隧道数据仓库"""

    @pytest.mark.asyncio
    async def test_create_tunnel(self, db_manager: DatabaseManager):
        """测试创建隧道"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.create(
                domain="test-agent",
                name="Test Agent",
                description="A test agent",
            )

            assert tunnel.id is not None
            assert tunnel.domain == "test-agent"
            assert tunnel.name == "Test Agent"
            assert tunnel.token.startswith("tun_")
            assert tunnel.enabled is True

    @pytest.mark.asyncio
    async def test_create_tunnel_with_custom_token(self, db_manager: DatabaseManager):
        """测试使用自定义令牌创建隧道"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.create(
                domain="custom-agent",
                token="custom_token_12345",
            )

            assert tunnel.token == "custom_token_12345"

    @pytest.mark.asyncio
    async def test_get_by_domain(self, db_manager: DatabaseManager):
        """测试根据域名获取隧道"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            await repo.create(domain="find-me")

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.get_by_domain("find-me")

            assert tunnel is not None
            assert tunnel.domain == "find-me"

    @pytest.mark.asyncio
    async def test_get_by_token(self, db_manager: DatabaseManager):
        """测试根据令牌获取隧道"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            created = await repo.create(domain="token-test", token="find_token")

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.get_by_token("find_token")

            assert tunnel is not None
            assert tunnel.domain == "token-test"

    @pytest.mark.asyncio
    async def test_list_all(self, db_manager: DatabaseManager):
        """测试列出所有隧道"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            await repo.create(domain="list-1")
            await repo.create(domain="list-2")
            await repo.create(domain="list-3")

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnels = await repo.list_all()

            assert len(tunnels) >= 3
            domains = [t.domain for t in tunnels]
            assert "list-1" in domains
            assert "list-2" in domains
            assert "list-3" in domains

    @pytest.mark.asyncio
    async def test_update_enabled(self, db_manager: DatabaseManager):
        """测试更新启用状态"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            await repo.create(domain="toggle-me")

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            result = await repo.update_enabled("toggle-me", False)
            assert result is True

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.get_by_domain("toggle-me")
            assert tunnel.enabled is False

    @pytest.mark.asyncio
    async def test_delete(self, db_manager: DatabaseManager):
        """测试删除隧道"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            await repo.create(domain="delete-me")

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            result = await repo.delete("delete-me")
            assert result is True

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.get_by_domain("delete-me")
            assert tunnel is None

    @pytest.mark.asyncio
    async def test_regenerate_token(self, db_manager: DatabaseManager):
        """测试重新生成令牌"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.create(domain="regen-token", token="old_token")
            old_token = tunnel.token

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            new_token = await repo.regenerate_token("regen-token")

            assert new_token is not None
            assert new_token != old_token
            assert new_token.startswith("tun_")

    @pytest.mark.asyncio
    async def test_increment_requests(self, db_manager: DatabaseManager):
        """测试增加请求计数"""
        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.create(domain="count-test", token="count_token")
            assert tunnel.total_requests == 0

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            await repo.increment_requests("count_token", 5)

        async with db_manager.session() as session:
            repo = TunnelRepository(session)
            tunnel = await repo.get_by_domain("count-test")
            assert tunnel.total_requests == 5
