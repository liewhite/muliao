"""触发器记上一次触发的收件方句柄（#631，归属表第 7 行）：last_session_id 换成 last_target（sessions.api.Recipient 的 JSON），
旧值换算成 {"to": "session", "session_id": <旧值>}；定时触发器 state["skipped"] 里挡路的会话 session_id 同样换成 target。
降级反算：句柄是一个会话的取回它的 id，按 key 或按发送方的置空（旧代码只认会话 id，置空等于「从没触发过」，下次照常触发）；
skipped 里的 target 不是一个会话的整段去掉（下次到点重新计这一轮）。

Revision ID: f4c1a9d27e83
Revises: e3a9c7b15d20
Create Date: 2026-11-11 21:00:00.000000
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "f4c1a9d27e83"
down_revision: str | None = "e3a9c7b15d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _loaded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def upgrade() -> None:
    op.add_column("triggers", sa.Column("last_target", sa.JSON(), nullable=True))
    conn = op.get_bind()
    for trigger_id, last_session_id, state in conn.execute(
        sa.text("SELECT id, last_session_id, state FROM triggers")
    ).all():
        target = None if last_session_id is None else {"to": "session", "session_id": last_session_id}
        state = dict(_loaded(state) or {})
        skipped = state.get("skipped")
        if isinstance(skipped, dict) and "session_id" in skipped:
            skipped = dict(skipped)
            skipped["target"] = {"to": "session", "session_id": skipped.pop("session_id")}
            state["skipped"] = skipped
        conn.execute(
            sa.text("UPDATE triggers SET last_target = :target, state = :state WHERE id = :id"),
            {
                "target": None if target is None else json.dumps(target),
                "state": json.dumps(state),
                "id": trigger_id,
            },
        )
    # 列上没有索引与约束，SQLite 原生 DROP COLUMN，不重建表（重建会丢掉 sqlite_autoincrement 与部分唯一索引）
    op.drop_column("triggers", "last_session_id")


def downgrade() -> None:
    op.add_column("triggers", sa.Column("last_session_id", sa.String(36), nullable=True))
    conn = op.get_bind()
    for trigger_id, last_target, state in conn.execute(
        sa.text("SELECT id, last_target, state FROM triggers")
    ).all():
        target = _loaded(last_target)
        session_id = (
            target["session_id"] if isinstance(target, dict) and target.get("to") == "session" else None
        )
        state = dict(_loaded(state) or {})
        skipped = state.get("skipped")
        if isinstance(skipped, dict) and "target" in skipped:
            skipped = dict(skipped)
            blocking = skipped.pop("target")
            if isinstance(blocking, dict) and blocking.get("to") == "session":
                skipped["session_id"] = blocking["session_id"]
                state["skipped"] = skipped
            else:
                state.pop("skipped")
        conn.execute(
            sa.text("UPDATE triggers SET last_session_id = :sid, state = :state WHERE id = :id"),
            {"sid": session_id, "state": json.dumps(state), "id": trigger_id},
        )
    op.drop_column("triggers", "last_target")
