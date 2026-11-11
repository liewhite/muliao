"""#648：领域事件改为在发布方事务里同步分发，定向命令改为直接调用，不再有投递任务（tasks.kind = 'event'）。

删掉全部 event 任务：已完成的只是记录；没完成的（平时都马上执行完，部署前确认为 0）在新版本里没有处理器，留着会挡住它们的
lane 并在启动时报错。

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
    op.get_bind().execute(sa.text("DELETE FROM tasks WHERE kind = 'event'"))


def downgrade() -> None:
    """删掉的投递任务不恢复。"""
