"""
WS-Tunnel 数据库模型

使用 SQLAlchemy 2.0 风格，支持：
- SQLite（开发/小规模）
- MySQL（生产）
- PostgreSQL（生产）
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, Index, func
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


class TunnelRequestLog(Base):
    """
    隧道请求日志表
    
    记录通过隧道转发的每个 HTTP 请求的详细信息，用于：
    - 调试和排查问题
    - 统计和分析
    - 审计追踪
    """
    __tablename__ = "tunnel_request_logs"
    
    # 主键
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    # 请求时间
    timestamp: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
        comment="请求时间"
    )
    
    # 隧道信息
    tunnel_domain: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="隧道域名"
    )
    
    # 请求信息
    method: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="HTTP 方法 (GET/POST/PUT/DELETE等)"
    )
    
    path: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        comment="请求路径（包含查询参数）"
    )
    
    # 请求头（JSON 格式存储）
    request_headers: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="请求头（JSON 格式）"
    )
    
    # 请求体
    request_body: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="请求体内容"
    )
    
    # 响应信息
    status_code: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="HTTP 状态码"
    )
    
    response_headers: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="响应头（JSON 格式）"
    )
    
    response_body: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="响应体内容（截断到 10000 字符）"
    )
    
    # 错误信息
    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="错误信息"
    )
    
    # 性能数据
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="请求耗时（毫秒）"
    )
    
    # 索引
    __table_args__ = (
        Index("idx_tunnel_request_logs_timestamp", "timestamp"),
        Index("idx_tunnel_request_logs_domain", "tunnel_domain"),
        Index("idx_tunnel_request_logs_status", "status_code"),
    )
    
    def __repr__(self) -> str:
        return f"<TunnelRequestLog(id={self.id}, domain={self.tunnel_domain}, method={self.method}, status={self.status_code})>"
    
    def to_dict(self) -> dict:
        """转换为字典（用于 API 返回）"""
        import json
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "tunnel_domain": self.tunnel_domain,
            "method": self.method,
            "path": self.path,
            "request_headers": json.loads(self.request_headers) if self.request_headers else None,
            "request_body": self.request_body[:500] if self.request_body else None,  # 只返回前 500 字符
            "status_code": self.status_code,
            "response_headers": json.loads(self.response_headers) if self.response_headers else None,
            "response_body": self.response_body[:500] if self.response_body else None,  # 只返回前 500 字符
            "error": self.error,
            "duration_ms": self.duration_ms,
        }
