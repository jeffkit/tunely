"""
WS-Tunnel 数据库模型

使用 SQLAlchemy 2.0 风格，支持：
- SQLite（开发/小规模）
- MySQL（生产）
- PostgreSQL（生产）
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 基类"""

    pass


class Tunnel(Base):
    """
    隧道注册表

    每个隧道对应一个域名和一个令牌，用于：
    1. 预注册隧道（管理员操作）
    2. Worker 连接时验证令牌
    3. 请求转发时根据域名查找隧道
    """

    __tablename__ = "tunnels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 隧道标识
    domain: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True, comment="隧道域名/标识"
    )
    token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, comment="连接令牌"
    )

    # 描述信息
    name: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="隧道名称（可选）"
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="隧道描述（可选）"
    )

    # 状态
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, onupdate=func.now(), nullable=True, comment="更新时间"
    )

    # 统计信息（可选）
    last_connected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="最后连接时间"
    )
    total_requests: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="总请求数"
    )

    def __repr__(self) -> str:
        return f"<Tunnel(domain={self.domain!r}, enabled={self.enabled})>"

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "domain": self.domain,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_connected_at": (
                self.last_connected_at.isoformat() if self.last_connected_at else None
            ),
            "total_requests": self.total_requests,
        }
