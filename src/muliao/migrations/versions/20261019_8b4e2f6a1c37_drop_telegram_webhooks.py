"""删除 Telegram 与 webhook 触发源；会话的投递渠道收敛为是否推送

Revision ID: 8b4e2f6a1c37
Revises: 5e1d7c3a9b20
Create Date: 2026-10-19 17:30:00

- sessions.delivery（none/webpush/telegram）→ notify（布尔），删除 delivery_target。
- 删除 webhooks 表。
- deliver task 的载荷去掉 channel、target 字段。

已有来自 Telegram / webhook 的会话，或尚未完成的 Telegram 投递时拒绝迁移：
这些数据在新结构里无法表达，不做静默删除或改写，由人决定如何处理。

不可降级：被删除的触发源不再有代码支持。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b4e2f6a1c37"
down_revision: str | None = "5e1d7c3a9b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_unrepresentable(conn: sa.Connection) -> None:
    sessions = conn.exec_driver_sql(
        "SELECT origin, count(*) FROM sessions WHERE origin IN ('telegram', 'webhook') GROUP BY origin"
    ).fetchall()
    deliveries = conn.exec_driver_sql(
        "SELECT count(*) FROM tasks WHERE kind = 'deliver' AND status != 'done' "
        "AND json_extract(payload, '$.channel') = 'telegram'"
    ).scalar()
    if sessions or deliveries:
        raise RuntimeError(
            "removed triggers still have data, refusing to migrate: "
            f"sessions by origin {dict(sessions)}, unfinished telegram deliveries {deliveries}. "
            "Delete those sessions (and tasks) first if they are no longer needed."
        )


def upgrade() -> None:
    conn = op.get_bind()
    _refuse_unrepresentable(conn)

    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("notify", sa.Boolean(), nullable=True))
    op.execute("UPDATE sessions SET notify = (delivery = 'webpush')")
    with op.batch_alter_table("sessions") as batch:
        batch.alter_column("notify", existing_type=sa.Boolean(), nullable=False)
        batch.drop_column("delivery")
        batch.drop_column("delivery_target")

    op.drop_table("webhooks")
    op.execute("UPDATE tasks SET payload = json_remove(payload, '$.channel', '$.target') WHERE kind = 'deliver'")


def downgrade() -> None:
    raise NotImplementedError("removing Telegram and webhook triggers is irreversible")
