"""Add invites.role for admin-role invites (console contract v1.1)

契约 v1.1（管理端鉴权方案 A）：邀请码可签发 admin 角色，注册按邀请码 role
落 users.role。invites 新增 role 列（默认 tenant）——纯加法迁移，存量行
server_default='tenant' 与原语义一致，旧库升级零影响。

Revision ID: 007
Revises: 006
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'invites',
        sa.Column(
            'role', sa.String(length=10), nullable=False,
            server_default='tenant',
            comment='受邀角色: tenant/admin（默认 tenant）',
        ),
    )


def downgrade() -> None:
    op.drop_column('invites', 'role')
