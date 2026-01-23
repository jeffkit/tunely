"""Initial migration - Create tunnels table

Revision ID: 001
Revises: 
Create Date: 2024-01-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tunnels',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=False, comment='隧道域名/标识'),
        sa.Column('token', sa.String(length=64), nullable=False, comment='连接令牌'),
        sa.Column('name', sa.String(length=100), nullable=True, comment='隧道名称（可选）'),
        sa.Column('description', sa.Text(), nullable=True, comment='隧道描述（可选）'),
        sa.Column('enabled', sa.Boolean(), nullable=False, default=True, comment='是否启用'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now(), comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), nullable=True, comment='更新时间'),
        sa.Column('last_connected_at', sa.DateTime(), nullable=True, comment='最后连接时间'),
        sa.Column('total_requests', sa.Integer(), nullable=False, default=0, comment='总请求数'),
        sa.PrimaryKeyConstraint('id'),
    )
    
    # 创建索引
    op.create_index('ix_tunnels_domain', 'tunnels', ['domain'], unique=True)
    op.create_index('ix_tunnels_token', 'tunnels', ['token'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_tunnels_token', table_name='tunnels')
    op.drop_index('ix_tunnels_domain', table_name='tunnels')
    op.drop_table('tunnels')
