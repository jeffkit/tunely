"""Add tunnel mode field

Revision ID: 003
Revises: 002
Create Date: 2026-01-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 添加 mode 字段，默认值为 'http'（保持向后兼容）
    op.add_column(
        'tunnels',
        sa.Column('mode', sa.String(length=10), nullable=False, server_default='http', comment='隧道模式: http/tcp')
    )


def downgrade() -> None:
    op.drop_column('tunnels', 'mode')
