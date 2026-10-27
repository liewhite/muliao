"""调用开出的 thread 记进自己的表 thread_calls（#251）：哪次工具调用（父会话 + 调用 id）开出或续写了哪个 thread，
在开出 thread 的事务里写入，是界面把 thread 挂在调用处的唯一来源。原先这件事记在两处：工具结果 metadata 的
`muliao_threads`（结果不是工具自己产出的——停止、失败重试、没等到回答——就没有），与委派记录（只给还没有结果的
ask_agent 用）。

数据：从这两处回填，按（父会话, 调用 id）去重，只收 thread 还在的：
- messages 里工具结果（part_kind 为 tool-return）metadata 的 `muliao_threads`：父会话是消息所在的会话；
- delegations：holder 为 `delegation:<父会话>:<调用 id>`，thread_session_id 非空。
两处对同一次调用给出不同 thread 时报错（不应出现）。metadata 原样留在旧消息里，之后不再读。

降级：删表（降级后的代码只认 metadata，这之后新开的 thread 在旧代码里不挂在调用处）。

Revision ID: 29411a27d5ec
Revises: c2d9e4b7a1f0
Create Date: 2026-10-27 15:38:33.757499
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "29411a27d5ec"
down_revision: str | None = "c2d9e4b7a1f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_THREADS = "muliao_threads"
_HOLDER_PREFIX = "delegation:"


def upgrade() -> None:
    op.create_table(
        "thread_calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_session_id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=100), nullable=False),
        sa.Column("thread_session_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["parent_session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["thread_session_id"],
            ["sessions.id"],
            name="fk_thread_calls_thread_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_thread_calls_thread_session_id", "thread_calls", ["thread_session_id"], unique=False)
    op.create_index("uq_thread_calls_call", "thread_calls", ["parent_session_id", "call_id"], unique=True)
    _backfill(op.get_bind())


def _backfill(conn: sa.Connection) -> None:
    sessions = set(conn.execute(sa.text("SELECT id FROM sessions")).scalars())
    found: dict[tuple[str, str], str] = {}

    def add(parent: str, call_id: str, thread: str) -> None:
        if parent not in sessions or thread not in sessions:
            return
        key = (parent, call_id)
        if found.setdefault(key, thread) != thread:
            raise RuntimeError(f"call {call_id} of session {parent} points to {found[key]} and {thread}")

    rows = conn.execute(
        sa.text("SELECT session_id, payload FROM messages WHERE payload LIKE :p ORDER BY id"),
        {"p": f"%{_THREADS}%"},
    )
    for session_id, payload in rows:
        for part in json.loads(payload).get("parts", []):
            if part.get("part_kind") != "tool-return" or not isinstance(part.get("metadata"), dict):
                continue
            for thread in part["metadata"].get(_THREADS, []):
                add(session_id, part["tool_call_id"], thread)
    rows = conn.execute(
        sa.text(
            "SELECT holder, parent_session_id, thread_session_id FROM delegations "
            "WHERE thread_session_id IS NOT NULL ORDER BY id"
        )
    )
    for holder, parent, thread in rows:
        prefix = f"{_HOLDER_PREFIX}{parent}:"
        if not holder.startswith(prefix):
            raise RuntimeError(f"delegation holder {holder!r} does not name its asker {parent}")
        add(parent, holder[len(prefix) :], thread)
    if found:
        conn.execute(
            sa.text(
                "INSERT INTO thread_calls (parent_session_id, call_id, thread_session_id) "
                "VALUES (:parent, :call_id, :thread)"
            ),
            [{"parent": p, "call_id": c, "thread": t} for (p, c), t in found.items()],
        )


def downgrade() -> None:
    op.drop_index("uq_thread_calls_call", table_name="thread_calls")
    op.drop_index("ix_thread_calls_thread_session_id", table_name="thread_calls")
    op.drop_table("thread_calls")
