"""
WS-Tunnel 数据仓库

提供隧道数据的 CRUD 操作
"""

import secrets
from datetime import datetime, timezone

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Tunnel


class TunnelRepository:
    """隧道数据仓库"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        domain: str,
        token: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Tunnel:
        """
        创建隧道

        Args:
            domain: 隧道域名/标识
            token: 连接令牌（可选，不提供则自动生成）
            name: 隧道名称（可选）
            description: 隧道描述（可选）

        Returns:
            创建的隧道对象
        """
        if not token:
            token = f"tun_{secrets.token_urlsafe(32)}"

        tunnel = Tunnel(
            domain=domain,
            token=token,
            name=name,
            description=description,
            enabled=True,
        )
        self.session.add(tunnel)
        await self.session.flush()
        return tunnel

    async def get_by_domain(self, domain: str) -> Tunnel | None:
        """根据域名获取隧道"""
        result = await self.session.execute(
            select(Tunnel).where(Tunnel.domain == domain)
        )
        return result.scalar_one_or_none()

    async def get_by_token(self, token: str) -> Tunnel | None:
        """根据令牌获取隧道"""
        result = await self.session.execute(
            select(Tunnel).where(Tunnel.token == token)
        )
        return result.scalar_one_or_none()

    async def list_all(
        self, enabled_only: bool = False, limit: int = 100, offset: int = 0
    ) -> list[Tunnel]:
        """
        列出所有隧道

        Args:
            enabled_only: 是否只返回启用的隧道
            limit: 返回数量限制
            offset: 偏移量
        """
        query = select(Tunnel).order_by(Tunnel.created_at.desc())
        if enabled_only:
            query = query.where(Tunnel.enabled == True)
        query = query.limit(limit).offset(offset)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def update_enabled(self, domain: str, enabled: bool) -> bool:
        """更新隧道启用状态"""
        result = await self.session.execute(
            update(Tunnel)
            .where(Tunnel.domain == domain)
            .values(enabled=enabled, updated_at=datetime.now(timezone.utc))
        )
        return result.rowcount > 0

    async def update_last_connected(self, token: str) -> bool:
        """更新最后连接时间"""
        result = await self.session.execute(
            update(Tunnel)
            .where(Tunnel.token == token)
            .values(last_connected_at=datetime.utcnow())
        )
        return result.rowcount > 0

    async def increment_requests(self, token: str, count: int = 1) -> bool:
        """增加请求计数"""
        result = await self.session.execute(
            update(Tunnel)
            .where(Tunnel.token == token)
            .values(total_requests=Tunnel.total_requests + count)
        )
        return result.rowcount > 0

    async def delete(self, domain: str) -> bool:
        """删除隧道 - 使用 SQL DELETE 语句"""
        stmt = delete(Tunnel).where(Tunnel.domain == domain)
        result = await self.session.execute(stmt)
        return result.rowcount > 0

    async def regenerate_token(self, domain: str) -> str | None:
        """重新生成令牌"""
        new_token = f"tun_{secrets.token_urlsafe(32)}"
        result = await self.session.execute(
            update(Tunnel)
            .where(Tunnel.domain == domain)
            .values(token=new_token, updated_at=datetime.now(timezone.utc))
        )
        if result.rowcount > 0:
            return new_token
        return None
