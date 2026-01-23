"""
WS-Tunnel 数据仓库

提供隧道数据的 CRUD 操作
"""

import secrets
from datetime import datetime, timezone

from typing import List, Optional
from sqlalchemy import select, update, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Tunnel, TunnelRequestLog


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


class TunnelRequestLogRepository:
    """隧道请求日志数据仓库"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create(
        self,
        tunnel_domain: str,
        method: str,
        path: str,
        request_headers: dict | None = None,
        request_body: str | None = None,
        status_code: int | None = None,
        response_headers: dict | None = None,
        response_body: str | None = None,
        error: str | None = None,
        duration_ms: int = 0,
    ) -> TunnelRequestLog:
        """创建请求日志记录"""
        import json
        
        log = TunnelRequestLog(
            tunnel_domain=tunnel_domain,
            method=method,
            path=path[:1000],  # 限制路径长度
            request_headers=json.dumps(request_headers) if request_headers else None,
            request_body=request_body[:10000] if request_body else None,  # 限制请求体长度
            status_code=status_code,
            response_headers=json.dumps(response_headers) if response_headers else None,
            response_body=response_body[:10000] if response_body else None,  # 限制响应体长度
            error=error[:2000] if error else None,  # 限制错误信息长度
            duration_ms=duration_ms,
        )
        self.session.add(log)
        await self.session.flush()
        await self.session.refresh(log)
        return log
    
    async def get_recent(
        self,
        tunnel_domain: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[TunnelRequestLog]:
        """获取最近的请求日志"""
        query = select(TunnelRequestLog).order_by(TunnelRequestLog.timestamp.desc())
        
        if tunnel_domain:
            query = query.where(TunnelRequestLog.tunnel_domain == tunnel_domain)
        
        query = query.limit(limit).offset(offset)
        
        result = await self.session.execute(query)
        return list(result.scalars().all())
    
    async def count(self, tunnel_domain: str | None = None) -> int:
        """统计请求日志数量"""
        query = select(func.count(TunnelRequestLog.id))
        
        if tunnel_domain:
            query = query.where(TunnelRequestLog.tunnel_domain == tunnel_domain)
        
        result = await self.session.execute(query)
        return result.scalar_one() or 0
