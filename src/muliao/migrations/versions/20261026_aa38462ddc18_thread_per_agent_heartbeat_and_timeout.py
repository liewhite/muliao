"""thread per agent, heartbeat and timeout

#217：
- sessions 的 thread 唯一索引 uq_sessions_thread 从（父会话, thread）改为（父会话, agent, thread）：给不同 agent 开
  同名 thread 是两个 thread。
- jobs 加 check_seconds（运行中检查的间隔，run_shell 的 heartbeat_seconds，非空）：已有行回填 60（当时全局固定的
  runtime.job_check_seconds 默认值，线上没改过）。
- session_holds 加 check_seconds（delegate_task(report_back) 的心跳间隔）与 deadline（ask_agent 的超时时刻），都可空。
  回填（会改数据）：report_back 的委派占用（holder 以 delegation: 开头、没有 call_id）check_seconds 置 60，与当时一致；
  ask_agent 的旧占用不设 deadline（发起时没有 timeout）。

降级：删列；恢复旧索引前，同一父会话里同名的 thread 只保留最早一个的名字，其余在名字后加 `#<agent id 前 8 位>`
（会改数据），否则旧的唯一索引建不起来。

全部用 SQLite 原生 ADD/DROP COLUMN（3.35+）与单独的索引语句，不重建表：jobs、session_holds 的 AUTOINCREMENT 与
sqlite_sequence 不受影响。

Revision ID: aa38462ddc18
Revises: e8964a50067c
Create Date: 2026-10-26 14:06:06.599937
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa38462ddc18"
down_revision: str | None = "e8964a50067c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HAS_PARENT = sa.text("parent_session_id IS NOT NULL")


def upgrade() -> None:
    op.add_column("jobs", sa.Column("check_seconds", sa.Double(), server_default="60", nullable=False))
    op.add_column("session_holds", sa.Column("check_seconds", sa.Double(), nullable=True))
    op.add_column("session_holds", sa.Column("deadline", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE session_holds SET check_seconds = 60 WHERE holder LIKE 'delegation:%' AND call_id IS NULL"
    )
    op.drop_index("uq_sessions_thread", table_name="sessions")
    op.create_index(
        "uq_sessions_thread",
        "sessions",
        ["parent_session_id", "agent_id", "thread"],
        unique=True,
        sqlite_where=_HAS_PARENT,
    )


def downgrade() -> None:
    op.drop_index("uq_sessions_thread", table_name="sessions")
    op.execute(
        # 旧代码的 thread 名最长 100：先截到 91 再加 9 个字符的后缀
        "UPDATE sessions SET thread = substr(thread, 1, 91) || '#' || substr(agent_id, 1, 8) "
        "WHERE parent_session_id IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM sessions o WHERE o.parent_session_id = sessions.parent_session_id "
        "AND o.thread = sessions.thread AND (o.created_at < sessions.created_at "
        "OR (o.created_at = sessions.created_at AND o.id < sessions.id)))"
    )
    op.create_index(
        "uq_sessions_thread",
        "sessions",
        ["parent_session_id", "thread"],
        unique=True,
        sqlite_where=_HAS_PARENT,
    )
    op.drop_column("session_holds", "deadline")
    op.drop_column("session_holds", "check_seconds")
    op.drop_column("jobs", "check_seconds")
