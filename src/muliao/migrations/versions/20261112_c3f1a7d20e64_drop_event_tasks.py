"""#648：领域事件改为在发布方事务里同步分发，定向命令改为直接调用，不再有投递任务（tasks.kind = 'event'）。

删掉已完成的 event 任务（只是记录）。还有没完成的（排着或失败的，平时都马上执行完）就拒绝升级：新版本没有处理它们的代码，
例如一条没走完的删除 agent 会让它永远停在「正在删除」，要先在旧版本里处理完（重试或人工收尾）再升级。

Revision ID: c3f1a7d20e64
Revises: b558e32d1821
Create Date: 2026-11-12 02:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f1a7d20e64"
down_revision: str | None = "b558e32d1821"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    unfinished = conn.execute(
        sa.text("SELECT id, status FROM tasks WHERE kind = 'event' AND status != 'done'")
    ).all()
    if unfinished:
        raise RuntimeError(
            f"{len(unfinished)} unfinished event deliveries must be handled with the previous version first: "
            f"{[(row.id, row.status) for row in unfinished]}"
        )
    conn.execute(sa.text("DELETE FROM tasks WHERE kind = 'event'"))


def downgrade() -> None:
    """删掉的投递任务不恢复。"""
