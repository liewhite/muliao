"""触发器脚本出错收成一列 faults（#580）：precheck_error、route_error 两列与流式 state["error"] 换算成 TriggerFault 列表
（{"source", "error", "address"?}），再删掉两列与 state 里的 error。降级反算回去（route 各条按「@route 地址: 原因」一行一条）。

Revision ID: b5e2c8d47a16
Revises: a7d3e5c91b04
Create Date: 2026-11-11 01:30:00.000000
"""

import json
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e2c8d47a16"
down_revision: str | None = "a7d3e5c91b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# route_error 的一行（triggers.service._route_error_line 写的）：「@route <地址>: <原因>」
_ROUTE_LINE = re.compile(r"^@route (\S+): (.*)$")


def upgrade() -> None:
    op.add_column("triggers", sa.Column("faults", sa.JSON(), nullable=False, server_default="[]"))
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, kind, state, precheck_error, route_error FROM triggers")).all()
    for trigger_id, kind, state, precheck_error, route_error in rows:
        state = json.loads(state) if isinstance(state, str) else dict(state or {})
        faults = []
        if precheck_error is not None:
            faults.append({"source": "precheck", "error": precheck_error})
        for line in (route_error or "").splitlines():
            m = _ROUTE_LINE.match(line)
            # 写入时总是这个格式；对不上的整行当原因、地址留空，不丢
            faults.append(
                {"source": "route", "error": m.group(2), "address": m.group(1)}
                if m
                else {"source": "route", "error": line, "address": None}
            )
        stream_error = state.pop("error", None) if kind == "stream" else None
        if stream_error is not None:
            faults.append({"source": "stream", "error": stream_error})
        conn.execute(
            sa.text("UPDATE triggers SET faults = :faults, state = :state WHERE id = :id"),
            {"faults": json.dumps(faults), "state": json.dumps(state), "id": trigger_id},
        )
    # 列上没有索引与约束，SQLite 原生 DROP COLUMN，不重建表（重建会丢掉 sqlite_autoincrement 与部分唯一索引）
    op.drop_column("triggers", "precheck_error")
    op.drop_column("triggers", "route_error")


def downgrade() -> None:
    op.add_column("triggers", sa.Column("precheck_error", sa.Text(), nullable=True))
    op.add_column("triggers", sa.Column("route_error", sa.Text(), nullable=True))
    conn = op.get_bind()
    for trigger_id, kind, state, faults in conn.execute(
        sa.text("SELECT id, kind, state, faults FROM triggers")
    ).all():
        state = json.loads(state) if isinstance(state, str) else dict(state or {})
        faults = json.loads(faults) if isinstance(faults, str) else list(faults or [])
        precheck = [f["error"] for f in faults if f["source"] == "precheck"]
        route = [f"@route {f.get('address')}: {f['error']}" for f in faults if f["source"] == "route"]
        stream = [f["error"] for f in faults if f["source"] == "stream"]
        if kind == "stream":
            state["error"] = stream[0] if stream else None
        conn.execute(
            sa.text(
                "UPDATE triggers SET precheck_error = :p, route_error = :r, state = :state WHERE id = :id"
            ),
            {
                "p": precheck[0] if precheck else None,
                "r": "\n".join(route) or None,
                "state": json.dumps(state),
                "id": trigger_id,
            },
        )
    op.drop_column("triggers", "faults")
