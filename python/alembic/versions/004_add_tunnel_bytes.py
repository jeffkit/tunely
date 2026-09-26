"""Add tunnel bytes_in/bytes_out columns

流量统计持久化：tunnels 表新增累计流量两列（跨重启累加）。
server_default='0' 保证存量行迁移后语义与新建行一致。

Revision ID: 004
Revises: 003
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '004'
down_revision: Union[str, None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tunnels',
        sa.Column(
            'bytes_in',
            sa.BigInteger(),
            nullable=False,
            server_default='0',
            comment='累计入流量（外部→内网，字节，跨重启累计）',
        ),
    )
    op.add_column(
        'tunnels',
        sa.Column(
            'bytes_out',
            sa.BigInteger(),
            nullable=False,
            server_default='0',
            comment='累计出流量（内网→外部，字节，跨重启累计）',
        ),
    )


def downgrade() -> None:
    op.drop_column('tunnels', 'bytes_out')
    op.drop_column('tunnels', 'bytes_in')
