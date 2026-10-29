"""去掉私有 thread（#294）：所有 thread 都公开，用 delegate_task 还是 ask_agent 续聊都行

只动数据，不改表结构：sessions.kind 为 private_thread 的改成 thread（之后出现在会话列表里、用户能写、删父会话时保留）。

降级不可逆：分不出哪些 thread 原来是 ask_agent 开的，全部保持 thread（旧代码按公开 thread 处理它们，只是不能再用
ask_agent 续聊）。

Revision ID: 9e3b1c7a5d20
Revises: 5387928cff43
Create Date: 2026-10-29 09:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9e3b1c7a5d20"
down_revision: str | None = "5387928cff43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE sessions SET kind = 'thread' WHERE kind = 'private_thread'")


def downgrade() -> None:
    pass
