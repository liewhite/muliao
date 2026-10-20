"""turn task 的 payload 增加 report_to（这一轮结束时把结果交回的会话，见 runtime.reports）

已有的 turn task 都不交回结果，补上 null，读取方不再需要区分新旧两种 payload。

Revision ID: a3339b22b82a
Revises: 21fe4c2a53ff
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a3339b22b82a"
down_revision: str | None = "21fe4c2a53ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE tasks SET payload = json_set(payload, '$.report_to', json('null')) WHERE kind = 'turn'"
    )


def downgrade() -> None:
    op.execute("UPDATE tasks SET payload = json_remove(payload, '$.report_to') WHERE kind = 'turn'")
