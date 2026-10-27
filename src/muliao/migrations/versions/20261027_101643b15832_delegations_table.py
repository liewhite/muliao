"""委派的发起参数拆到自己的表 delegations（#244）：session_holds 上只给委派用的 check_seconds / deadline 两列搬进
delegations（每项委派一行，按 holder 唯一），再加上做这项委派的 thread（thread_session_id，原先由 thread 一侧以 holder
为关联 id 的信件推出）；然后删掉 session_holds 的这两列（SQLite 原生 DROP COLUMN，不重建表，AUTOINCREMENT 与
sqlite_sequence 不受影响）。

数据：每个委派占用（holder 以 delegation: 开头，不论状态）搬一行：发起会话、心跳间隔、超时时刻、登记时刻原样，thread 取
以它为关联 id 的最早一封信件所在的会话（thread 已被删除、信件随之删除的为 NULL，与原先推不出 thread 一致）。

降级：session_holds 加回两列（可空），按 holder 从 delegations 搬回，删表。降级再升级时 delegations.created_at 取占用的
created_at（与原值差几微秒，只让 deadline − created_at 算出的超时秒数差同样的量）。

Revision ID: 101643b15832
Revises: abea1558f93c
Create Date: 2026-10-27 13:57:24.762718
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "101643b15832"
down_revision: str | None = "abea1558f93c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delegations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("holder", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("thread_session_id", sa.String(length=36), nullable=True),
        sa.Column("check_seconds", sa.Double(), nullable=True),
        sa.Column("deadline", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["thread_session_id"],
            ["sessions.id"],
            name="fk_delegations_thread_session_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delegations_session_id", "delegations", ["session_id"], unique=False)
    op.create_index("ix_delegations_thread_session_id", "delegations", ["thread_session_id"], unique=False)
    op.create_index("uq_delegations_holder", "delegations", ["holder"], unique=True)
    op.execute(
        "INSERT INTO delegations (holder, session_id, thread_session_id, check_seconds, deadline, created_at) "
        "SELECT h.holder, h.session_id, "
        "(SELECT m.session_id FROM mail m WHERE m.correlation = h.holder ORDER BY m.id LIMIT 1), "
        "h.check_seconds, h.deadline, h.created_at "
        "FROM session_holds h WHERE h.holder LIKE 'delegation:%' ORDER BY h.id"
    )
    op.drop_column("session_holds", "deadline")
    op.drop_column("session_holds", "check_seconds")


def downgrade() -> None:
    op.add_column("session_holds", sa.Column("check_seconds", sa.Double(), nullable=True))
    op.add_column("session_holds", sa.Column("deadline", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE session_holds SET "
        "check_seconds = (SELECT d.check_seconds FROM delegations d WHERE d.holder = session_holds.holder), "
        "deadline = (SELECT d.deadline FROM delegations d WHERE d.holder = session_holds.holder) "
        "WHERE holder IN (SELECT holder FROM delegations)"
    )
    op.drop_index("uq_delegations_holder", table_name="delegations")
    op.drop_index("ix_delegations_thread_session_id", table_name="delegations")
    op.drop_index("ix_delegations_session_id", table_name="delegations")
    op.drop_table("delegations")
