"""#645：删触发器的投递目标与 @route、webhook 触发方式、单个会话的压缩阈值。

- triggers：route 列删掉（所有触发都新开自己 agent 的会话）；last_target（会话领域的收件方句柄 JSON）换成 last_session_id
  （句柄里的会话 id；按 key、按发送方的句柄只来自已删的投递目标，换算后为空，即按从没触发过算不重叠）；state.skipped 里挡路的
  句柄 target 同样换成 session_id（不是会话句柄的，这一轮跳过记录清掉）；faults 里写错地址（source=route）的条目删掉、其余
  去掉 address 键；webhook 触发器删掉。
- issue_activities：触发器投进 issue 的事件动态（kind=event）删掉（线上只有 3 条自检留下的）。
- sessions：compaction_threshold_tokens 列删掉（全部会话用全局阈值）。

Revision ID: b558e32d1821
Revises: 0c6e2b9f41d8
Create Date: 2026-11-12 00:51:44.471215
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b558e32d1821"
down_revision: str | None = "0c6e2b9f41d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# triggers.id 是会话的发送方身份，永不复用（AUTOINCREMENT）：batch 重建表时反射不出这个选项，要显式给出；重建后
# sqlite_sequence 只按现存的最大 id 算，删掉过的更大 id 会被复用，所以重建前记下、重建后补回（_keep_sequence）
_AUTOINCREMENT = {"sqlite_autoincrement": True}


def _sequence(conn: sa.Connection) -> int:
    return conn.execute(
        sa.text("SELECT coalesce(max(seq), 0) FROM sqlite_sequence WHERE name = 'triggers'")
    ).scalar_one()


def _keep_sequence(conn: sa.Connection, before: int) -> None:
    conn.execute(
        sa.text("UPDATE sqlite_sequence SET seq = max(seq, :before) WHERE name = 'triggers'"),
        {"before": before},
    )
    if (
        conn.execute(sa.text("SELECT count(*) FROM sqlite_sequence WHERE name = 'triggers'")).scalar_one()
        == 0
    ):
        conn.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('triggers', :before)"),
            {"before": before},
        )


def _rewrite_trigger_json(conn: sa.Connection) -> None:
    """state.skipped 的句柄与 faults 的地址（JSON 内部的形状，逐行在 Python 里换算）。"""
    rows = conn.execute(sa.text("SELECT id, state, faults FROM triggers")).all()
    for trigger_id, state_raw, faults_raw in rows:
        state = json.loads(state_raw) if isinstance(state_raw, str) else state_raw
        faults = json.loads(faults_raw) if isinstance(faults_raw, str) else (faults_raw or [])
        new_state = state
        skipped = (state or {}).get("skipped")
        if isinstance(skipped, dict) and "target" in skipped:
            target = skipped.pop("target")
            if isinstance(target, dict) and target.get("to") == "session":
                new_state = {**state, "skipped": {**skipped, "session_id": target["session_id"]}}
            else:
                new_state = {**state, "skipped": None}
        new_faults = [
            {k: v for k, v in f.items() if k != "address"} for f in faults if f.get("source") != "route"
        ]
        if new_state != state or new_faults != faults:
            conn.execute(
                sa.text("UPDATE triggers SET state = :state, faults = :faults WHERE id = :id"),
                {"state": json.dumps(new_state), "faults": json.dumps(new_faults), "id": trigger_id},
            )


def upgrade() -> None:
    conn = op.get_bind()
    sequence = _sequence(conn)
    conn.execute(sa.text("DELETE FROM triggers WHERE kind = 'webhook'"))
    conn.execute(sa.text("DELETE FROM issue_activities WHERE kind = 'event'"))
    _rewrite_trigger_json(conn)

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("compaction_threshold_tokens")

    with op.batch_alter_table("triggers", schema=None, table_kwargs=_AUTOINCREMENT) as batch_op:
        batch_op.add_column(sa.Column("last_session_id", sa.String(length=36), nullable=True))
    conn.execute(
        sa.text(
            "UPDATE triggers SET last_session_id = json_extract(last_target, '$.session_id') "
            "WHERE json_extract(last_target, '$.to') = 'session'"
        )
    )
    with op.batch_alter_table("triggers", schema=None, table_kwargs=_AUTOINCREMENT) as batch_op:
        batch_op.drop_column("route")
        batch_op.drop_column("last_target")
    _keep_sequence(conn, sequence)


def downgrade() -> None:
    """只恢复表结构与 last_target（由 last_session_id 换回会话句柄）；删掉的 webhook 触发器、事件动态、投递目标不恢复。"""
    conn = op.get_bind()
    sequence = _sequence(conn)
    with op.batch_alter_table("triggers", schema=None, table_kwargs=_AUTOINCREMENT) as batch_op:
        batch_op.add_column(sa.Column("last_target", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("route", sa.JSON(), nullable=True))
    conn.execute(
        sa.text(
            "UPDATE triggers SET last_target = json_object('to', 'session', 'session_id', last_session_id) "
            "WHERE last_session_id IS NOT NULL"
        )
    )
    # 跳过记录里的挡路会话是新形状，旧代码读不了：清掉这一轮（下次到点重新判断）
    conn.execute(
        sa.text(
            "UPDATE triggers SET state = json_set(state, '$.skipped', json('null')) "
            "WHERE json_extract(state, '$.skipped') IS NOT NULL"
        )
    )
    with op.batch_alter_table("triggers", schema=None, table_kwargs=_AUTOINCREMENT) as batch_op:
        batch_op.drop_column("last_session_id")
    _keep_sequence(conn, sequence)

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("compaction_threshold_tokens", sa.Integer(), nullable=True))
