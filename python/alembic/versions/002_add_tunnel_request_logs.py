"""Add tunnel_request_logs table

Revision ID: 002
Revises: 001
Create Date: 2026-01-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tunnel_request_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False, comment='请求时间'),
        sa.Column('tunnel_domain', sa.String(length=255), nullable=False, comment='隧道域名'),
        sa.Column('method', sa.String(length=10), nullable=False, comment='HTTP 方法 (GET/POST/PUT/DELETE等)'),
        sa.Column('path', sa.String(length=1000), nullable=False, comment='请求路径（包含查询参数）'),
        sa.Column('request_headers', sa.Text(), nullable=True, comment='请求头（JSON 格式）'),
        sa.Column('request_body', sa.Text(), nullable=True, comment='请求体内容'),
        sa.Column('status_code', sa.Integer(), nullable=True, comment='HTTP 状态码'),
        sa.Column('response_headers', sa.Text(), nullable=True, comment='响应头（JSON 格式）'),
        sa.Column('response_body', sa.Text(), nullable=True, comment='响应体内容（截断到 10000 字符）'),
        sa.Column('error', sa.Text(), nullable=True, comment='错误信息'),
        sa.Column('duration_ms', sa.Integer(), nullable=False, default=0, comment='请求耗时（毫秒）'),
        sa.PrimaryKeyConstraint('id'),
    )
    
    # 创建索引
    op.create_index('idx_tunnel_request_logs_timestamp', 'tunnel_request_logs', ['timestamp'], unique=False)
    op.create_index('idx_tunnel_request_logs_domain', 'tunnel_request_logs', ['tunnel_domain'], unique=False)
    op.create_index('idx_tunnel_request_logs_status', 'tunnel_request_logs', ['status_code'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_tunnel_request_logs_status', table_name='tunnel_request_logs')
    op.drop_index('idx_tunnel_request_logs_domain', table_name='tunnel_request_logs')
    op.drop_index('idx_tunnel_request_logs_timestamp', table_name='tunnel_request_logs')
    op.drop_table('tunnel_request_logs')
