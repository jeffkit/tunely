"""Add multi-tenant console: users + invites tables, tunnels.owner_id

多租户自助控制台（docs/CONSOLE_MULTITENANT.md §3）：
- 新增 users / invites 两张表；
- tunnels 新增可空列 owner_id（FK users.id）——NULL 视为 admin/遗留所有，
  存量行不受影响（admin key 通道行为零变化）。

旧库升级零影响：纯加法迁移，不改既有列、不写存量数据。

Revision ID: 006
Revises: 005
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            'username', sa.String(length=32), nullable=False,
            comment='用户名（3-32 位，[a-z0-9_-]）',
        ),
        sa.Column(
            'password_hash', sa.String(length=255), nullable=False,
            comment='scrypt 密码哈希（格式 scrypt$n$r$p$salt$hash，hex 存储）',
        ),
        sa.Column(
            'role', sa.String(length=10), nullable=False,
            server_default='tenant', comment='角色: admin/tenant',
        ),
        sa.Column(
            'disabled', sa.Boolean(), nullable=False,
            server_default=sa.false(), comment='是否禁用',
        ),
        sa.Column(
            'created_at', sa.DateTime(), nullable=False,
            server_default=sa.func.now(), comment='创建时间',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username', name='uq_users_username'),
    )
    op.create_index('ix_users_username', 'users', ['username'], unique=True)

    op.create_table(
        'invites',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            'code', sa.String(length=64), nullable=False,
            comment='邀请码（可读格式）',
        ),
        sa.Column(
            'created_by', sa.String(length=32), nullable=False,
            server_default='admin', comment='签发者标识',
        ),
        sa.Column(
            'max_uses', sa.Integer(), nullable=False,
            server_default='1', comment='最大使用次数',
        ),
        sa.Column(
            'used_count', sa.Integer(), nullable=False,
            server_default='0', comment='已使用次数',
        ),
        sa.Column(
            'expires_at', sa.DateTime(), nullable=True,
            comment='过期时间（UTC；NULL = 永不过期）',
        ),
        sa.Column(
            'created_at', sa.DateTime(), nullable=False,
            server_default=sa.func.now(), comment='创建时间',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_invites_code'),
    )
    op.create_index('ix_invites_code', 'invites', ['code'], unique=True)

    # tunnels.owner_id：batch 模式兼顾 SQLite（不支持 ALTER ADD CONSTRAINT，
    # 走 copy-and-move 重建）与 MySQL/PostgreSQL（直发 ALTER）
    with op.batch_alter_table('tunnels') as batch:
        batch.add_column(
            sa.Column(
                'owner_id', sa.Integer(), nullable=True,
                comment='所有者用户 id（users.id）；NULL = admin/遗留所有',
            )
        )
        batch.create_foreign_key(
            'fk_tunnels_owner_id_users', 'users', ['owner_id'], ['id']
        )
    op.create_index('ix_tunnels_owner_id', 'tunnels', ['owner_id'])


def downgrade() -> None:
    op.drop_index('ix_tunnels_owner_id', table_name='tunnels')
    # SQLite 不支持直接 DROP FOREIGN KEY，走 batch 重建表
    with op.batch_alter_table('tunnels') as batch:
        batch.drop_constraint('fk_tunnels_owner_id_users', type_='foreignkey')
        batch.drop_column('owner_id')
    op.drop_index('ix_invites_code', table_name='invites')
    op.drop_table('invites')
    op.drop_index('ix_users_username', table_name='users')
    op.drop_table('users')
