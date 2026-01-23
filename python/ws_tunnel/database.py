"""
WS-Tunnel 数据库管理

提供异步数据库连接和会话管理
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    数据库管理器

    支持：
    - SQLite（开发/小规模）
    - MySQL（生产）
    - PostgreSQL（生产）
    """

    def __init__(self, database_url: str):
        """
        初始化数据库管理器

        Args:
            database_url: 数据库连接 URL
                - SQLite: sqlite+aiosqlite:///./data/tunnels.db
                - MySQL: mysql+aiomysql://user:pass@host/db
                - PostgreSQL: postgresql+asyncpg://user:pass@host/db
        """
        self.database_url = database_url
        self._engine = None
        self._session_factory = None

    async def initialize(self) -> None:
        """初始化数据库连接"""
        # 确保 SQLite 数据目录存在
        if "sqlite" in self.database_url:
            db_path = self.database_url.split("///")[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # 创建引擎
        self._engine = create_async_engine(
            self.database_url,
            echo=False,
            pool_pre_ping=True,
        )

        # 创建会话工厂
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

        # 创建表
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info(f"数据库初始化完成: {self.database_url}")

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._engine:
            await self._engine.dispose()
            logger.info("数据库连接已关闭")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        获取数据库会话

        Usage:
            async with db_manager.session() as session:
                # 使用 session
        """
        if not self._session_factory:
            raise RuntimeError("DatabaseManager not initialized. Call initialize() first.")

        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# 全局数据库管理器实例（可选）
_db_manager: DatabaseManager | None = None


async def init_database(database_url: str) -> DatabaseManager:
    """初始化数据库"""
    global _db_manager
    _db_manager = DatabaseManager(database_url)
    await _db_manager.initialize()
    return _db_manager


async def close_database() -> None:
    """关闭数据库连接"""
    global _db_manager
    if _db_manager:
        await _db_manager.close()
        _db_manager = None


def get_db_manager() -> DatabaseManager:
    """获取数据库管理器"""
    if not _db_manager:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db_manager
