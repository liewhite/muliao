"""turn task 的 payload 增加 answers（对挂起提问的回答，见 runtime.questions）

已有的 turn task 都没有回答，补上空对象，读取方不再需要区分新旧两种 payload。

Revision ID: b7c41e2d9a10
Revises: 9dda44ebb470
Create Date: 2026-10-19 23:45:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7c41e2d9a10"
down_revision: str | None = "9dda44ebb470"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE tasks SET payload = json_set(payload, '$.answers', json('{}')) WHERE kind = 'turn'")


def downgrade() -> None:
    op.execute("UPDATE tasks SET payload = json_remove(payload, '$.answers') WHERE kind = 'turn'")
