"""删除 Telegram 与 webhook 触发源；是否推送改由会话来源决定

Revision ID: 8b4e2f6a1c37
Revises: 5e1d7c3a9b20
Create Date: 2026-10-19 17:30:00

- 删除 sessions.delivery、sessions.delivery_target：通知只剩 Web Push，是否推送由 Origin.notifies 按来源决定
  （user、delegation 推送，cron 不推送），不再逐会话存储。
- 删除 webhooks 表。
- Web Push 投递任务的载荷去掉 channel、target 字段（Notification 只剩 title/body/session_id）。
  已完成的 Telegram 投递是历史记录，不会再被执行，原样保留，不改写成 Web Push 的样子。

以下数据在新结构里无法表达，遇到时拒绝迁移，由人决定如何处理，不做静默删除或改写：
- 来自 Telegram / webhook 的会话；
- delivery 与按来源推出的推送策略不一致的会话（例如不推送的用户会话）；
- 尚未完成的 Telegram 投递（含不属于任何会话的系统提示）。

不可降级：被删除的触发源不再有代码支持。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b4e2f6a1c37"
down_revision: str | None = "5e1d7c3a9b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 冻结的推送策略副本（与 models.Origin.notifies 一致）：迁移不随应用代码变化
_EXPECTED_DELIVERY = "CASE origin WHEN 'cron' THEN 'none' ELSE 'webpush' END"


def _refuse_unrepresentable(conn: sa.Connection) -> None:
    removed_origins = dict(
        conn.exec_driver_sql(
            "SELECT origin, count(*) FROM sessions WHERE origin IN ('telegram', 'webhook') GROUP BY origin"
        ).fetchall()
    )
    inconsistent = conn.exec_driver_sql(
        f"SELECT count(*) FROM sessions WHERE origin NOT IN ('telegram', 'webhook') "
        f"AND delivery != {_EXPECTED_DELIVERY}"
    ).scalar()
    telegram_pending = conn.exec_driver_sql(
        "SELECT count(*) FROM tasks WHERE kind = 'deliver' AND status != 'done' "
        "AND json_extract(payload, '$.channel') = 'telegram'"
    ).scalar()
    if removed_origins or inconsistent or telegram_pending:
        raise RuntimeError(
            "data not representable after removing Telegram/webhook, refusing to migrate: "
            f"sessions by removed origin {removed_origins}, "
            f"sessions whose delivery differs from their origin's policy {inconsistent}, "
            f"unfinished telegram deliveries {telegram_pending}. "
            "Delete or finish them first if they are no longer needed."
        )


def upgrade() -> None:
    _refuse_unrepresentable(op.get_bind())

    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("delivery")
        batch.drop_column("delivery_target")

    op.drop_table("webhooks")
    op.execute(
        "UPDATE tasks SET payload = json_remove(payload, '$.channel', '$.target') "
        "WHERE kind = 'deliver' AND json_extract(payload, '$.channel') = 'webpush'"
    )


def downgrade() -> None:
    raise NotImplementedError("removing Telegram and webhook triggers is irreversible")
