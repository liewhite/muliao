"""delegations 并进 thread_calls（#546）：一项委派就是一次要交回结局的调用，它的记录（占用者、心跳间隔）记在这次调用
开出 thread 的那一行上，删掉 delegations 表。

升级：thread_calls 加可空列 holder（按非空部分唯一索引）、check_seconds；按「holder = delegation:<发起会话>:<调用 id>」
把 delegations 的两列搬到对应行；delegations 里每个 thread 还在的委派都必须对得上一行（#251 起同一事务写两处），对不上
就报错不升级。thread 已删除（thread_session_id 为空）的委派早已结清，只是历史，随表删除。

降级：建回 delegations，从带 holder 的行搬回，created_at 取发起方占用的登记时刻（占用随发起会话删除，与行同在）；删两列。
升级时丢掉的「thread 已删除」的委派不恢复（旧代码只在等待项、停止时读它们，那时占用早已不 active）。

Revision ID: 5d1c8e2a7f46
Revises: 3b90f72a224e
Create Date: 2026-11-10 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d1c8e2a7f46"
down_revision: str | None = "3b90f72a224e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MATCH = "d.holder = 'delegation:' || thread_calls.parent_session_id || ':' || thread_calls.call_id"


def upgrade() -> None:
    conn = op.get_bind()
    unmatched = conn.execute(
        sa.text(
            "SELECT d.holder FROM delegations d WHERE d.thread_session_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM thread_calls WHERE thread_calls.thread_session_id = d.thread_session_id "
            f"AND {_MATCH})"
        )
    ).all()
    if unmatched:
        raise RuntimeError(f"delegations without their thread call: {[h for (h,) in unmatched][:5]}")
    op.add_column("thread_calls", sa.Column("holder", sa.String(length=200), nullable=True))
    op.add_column("thread_calls", sa.Column("check_seconds", sa.Double(), nullable=True))
    conn.execute(
        sa.text(
            "UPDATE thread_calls SET "
            f"holder = (SELECT d.holder FROM delegations d WHERE {_MATCH}), "
            f"check_seconds = (SELECT d.check_seconds FROM delegations d WHERE {_MATCH}) "
            f"WHERE EXISTS (SELECT 1 FROM delegations d WHERE {_MATCH})"
        )
    )
    op.create_index(
        "uq_thread_calls_holder",
        "thread_calls",
        ["holder"],
        unique=True,
        sqlite_where=sa.text("holder IS NOT NULL"),
    )
    op.drop_index("uq_delegations_holder", table_name="delegations")
    op.drop_index("ix_delegations_thread_session_id", table_name="delegations")
    op.drop_index("ix_delegations_parent_session_id", table_name="delegations")
    op.drop_table("delegations")


def downgrade() -> None:
    op.create_table(
        "delegations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("holder", sa.String(length=200), nullable=False),
        sa.Column("parent_session_id", sa.String(length=36), nullable=False),
        sa.Column("thread_session_id", sa.String(length=36), nullable=True),
        sa.Column("check_seconds", sa.Double(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["parent_session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["thread_session_id"],
            ["sessions.id"],
            ondelete="SET NULL",
            name="fk_delegations_thread_session_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delegations_parent_session_id", "delegations", ["parent_session_id"], unique=False)
    op.create_index("ix_delegations_thread_session_id", "delegations", ["thread_session_id"], unique=False)
    op.create_index("uq_delegations_holder", "delegations", ["holder"], unique=True)
    op.get_bind().execute(
        sa.text(
            "INSERT INTO delegations (holder, parent_session_id, thread_session_id, check_seconds, created_at) "
            "SELECT t.holder, t.parent_session_id, t.thread_session_id, t.check_seconds, h.created_at "
            "FROM thread_calls t JOIN session_holds h ON h.holder = t.holder "
            "WHERE t.holder IS NOT NULL ORDER BY h.id"
        )
    )
    op.drop_index("uq_thread_calls_holder", table_name="thread_calls")
    op.drop_column("thread_calls", "check_seconds")
    op.drop_column("thread_calls", "holder")
