"""Normalize tunnel mode to tcp (TCP-only convergence)

Revision ID: 005
Revises: 004
Create Date: 2026-09-30

docs/MIGRATION_TCP_ONLY.md §4.4：HTTP 模式退役（0.11 deprecation，1.0 removal），
存量隧道的 mode 统一归一为 'tcp'。纯数据迁移，不改表结构。

注意（单向迁移）：0.10 服务端在注册时缓存 mode 并据此路由 forward()，
归一后回滚旧二进制会使原 http 隧道的 /t/ 转发失效——执行本迁移前请
备份 mode 列（如 CREATE TABLE _backup_tunnel_mode AS SELECT id, mode FROM tunnels）。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE tunnels SET mode = 'tcp' WHERE mode <> 'tcp'")


def downgrade() -> None:
    # 刻意 no-op：归一后无法还原「哪些隧道原本是 http」，
    # 回滚请用升级前的 mode 列备份手工恢复（见文件头说明）。
    pass
