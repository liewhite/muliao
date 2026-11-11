"""信交给哪次运行记在信上、委派只由 thread_calls 行表示（#630，归属表第 3、4 行）。

升级：
- mail.consumed_by 改名 taken_by（信交给了哪次运行）；还排着、id 不大于所在会话最近一次运行 payload 里 through_mail_id
  的信件补上 taken_by = 那次运行（旧规则「id 不大于即交过」）；运行的 payload 清空。
- mail.correlation（字符串 delegation:<发起会话>:<调用 id>）换成 delegation_id（外键指向 thread_calls 行）：对得上行的
  填行 id，对不上的（发起会话已删除，行随之删除）置空——这些信件都已结清，置空与「发起方已删除」等价；删
  correlation_settled（结清没有改由占用是否 active 推导）。
- session_holds 加 thread_call_id（外键），委派的占用按 holder 对上行填上、holder 置空（holder 改为可空，只留给外部
  工作）；对不上行的委派占用（thread 已删除）holder 也置空，只剩 call_id 这一永久事实。还 active 却对不上行的委派
  占用是坏数据，拒绝升级。
- 删 thread_calls.holder。

降级反向还原：holder 字符串按行重新拼出（对不上行的委派占用拼成 delegation:<会话>:hold<id>，只为满足非空唯一），
correlation_settled = 占用不再 active；taken_by 改回 consumed_by，只留已处理的信件上的；运行的 through_mail_id 取交给
它的信件里最大的 id（没有为 0）。升级时置空的 23 封左右旧信件的 correlation 不恢复（已结清、已处理，旧代码不再读它们）。

Revision ID: e3a9c7b15d20
Revises: c0fe84d4f804
Create Date: 2026-11-11 20:30:00
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision: str = "e3a9c7b15d20"
down_revision: str | None = "c0fe84d4f804"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@contextmanager
def _keeping_mail_sequence(conn: sa.Connection) -> Iterator[None]:
    """batch 重建 mail 时 sqlite_sequence 会退回到现存最大 id：重建后写回原值，删掉的末尾 id 不被复用。"""
    seq = conn.execute(sa.text("SELECT seq FROM sqlite_sequence WHERE name = 'mail'")).scalar()
    yield
    if seq is not None:
        conn.execute(
            sa.text("UPDATE sqlite_sequence SET seq = max(seq, :seq) WHERE name = 'mail'"), {"seq": seq}
        )


def upgrade() -> None:
    conn = op.get_bind()
    broken = conn.execute(
        sa.text(
            "SELECT h.holder FROM session_holds h WHERE h.holder LIKE 'delegation:%' AND h.status = 'active' "
            "AND NOT EXISTS (SELECT 1 FROM thread_calls t WHERE t.holder = h.holder)"
        )
    ).all()
    if broken:
        raise RuntimeError(f"active delegation holds without their thread call: {[h for (h,) in broken][:5]}")

    # 信交给哪次运行：原生改列名（mail 带 AUTOINCREMENT，不走 batch 重建），索引随之改写
    op.execute("ALTER TABLE mail RENAME COLUMN consumed_by TO taken_by")
    op.drop_index("ix_mail_consumed_by", table_name="mail")
    op.create_index("ix_mail_taken_by", "mail", ["taken_by"], sqlite_where=sa.text("taken_by IS NOT NULL"))
    latest = "SELECT max(t.id) FROM tasks t WHERE t.kind = 'run' AND t.session_id = mail.session_id"
    conn.execute(
        sa.text(
            f"UPDATE mail SET taken_by = ({latest}) WHERE state = 'queued' AND taken_by IS NULL "
            f"AND id <= (SELECT json_extract(t.payload, '$.through_mail_id') FROM tasks t WHERE t.id = ({latest}))"
        )
    )
    conn.execute(sa.text("UPDATE tasks SET payload = '{}' WHERE kind = 'run'"))

    # 委派来的信件引用委派行
    # 带具名外键的新列：重建 mail（几千行）
    with (
        _keeping_mail_sequence(conn),
        op.batch_alter_table("mail", table_kwargs={"sqlite_autoincrement": True}) as batch,
    ):
        batch.add_column(sa.Column("delegation_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_mail_delegation_id", "thread_calls", ["delegation_id"], ["id"], ondelete="SET NULL"
        )
    conn.execute(
        sa.text(
            "UPDATE mail SET delegation_id = (SELECT t.id FROM thread_calls t WHERE t.holder = mail.correlation) "
            "WHERE correlation IS NOT NULL"
        )
    )
    op.drop_index("ix_mail_correlation", table_name="mail")
    op.drop_column("mail", "correlation")
    op.drop_column("mail", "correlation_settled")
    op.create_index(
        "ix_mail_delegation_id", "mail", ["delegation_id"], sqlite_where=sa.text("delegation_id IS NOT NULL")
    )

    # 委派的占用引用委派行
    with op.batch_alter_table("session_holds") as batch:
        batch.alter_column("holder", existing_type=sa.String(length=200), nullable=True)
        batch.add_column(sa.Column("thread_call_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_session_holds_thread_call_id", "thread_calls", ["thread_call_id"], ["id"], ondelete="SET NULL"
        )
    conn.execute(
        sa.text(
            "UPDATE session_holds SET "
            "thread_call_id = (SELECT t.id FROM thread_calls t WHERE t.holder = session_holds.holder), "
            "holder = NULL WHERE holder LIKE 'delegation:%'"
        )
    )
    op.create_index(
        "uq_session_holds_thread_call_id",
        "session_holds",
        ["thread_call_id"],
        unique=True,
        sqlite_where=sa.text("thread_call_id IS NOT NULL"),
    )

    op.drop_index("uq_thread_calls_holder", table_name="thread_calls")
    op.drop_column("thread_calls", "holder")


def downgrade() -> None:
    conn = op.get_bind()
    op.add_column("thread_calls", sa.Column("holder", sa.String(length=200), nullable=True))
    conn.execute(
        sa.text(
            "UPDATE thread_calls SET holder = 'delegation:' || parent_session_id || ':' || call_id "
            "WHERE EXISTS (SELECT 1 FROM session_holds h WHERE h.thread_call_id = thread_calls.id)"
        )
    )
    op.create_index(
        "uq_thread_calls_holder",
        "thread_calls",
        ["holder"],
        unique=True,
        sqlite_where=sa.text("holder IS NOT NULL"),
    )

    op.add_column("mail", sa.Column("correlation", sa.String(length=200), nullable=True))
    op.add_column(
        "mail", sa.Column("correlation_settled", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    conn.execute(
        sa.text(
            "UPDATE mail SET "
            "correlation = (SELECT t.holder FROM thread_calls t WHERE t.id = mail.delegation_id), "
            "correlation_settled = NOT EXISTS (SELECT 1 FROM session_holds h "
            "WHERE h.thread_call_id = mail.delegation_id AND h.status = 'active') "
            "WHERE delegation_id IS NOT NULL"
        )
    )
    op.create_index(
        "ix_mail_correlation", "mail", ["correlation"], sqlite_where=sa.text("correlation IS NOT NULL")
    )
    op.drop_index("ix_mail_delegation_id", table_name="mail")
    # 带外键的列 SQLite 不能原生删除：重建 mail
    with (
        _keeping_mail_sequence(conn),
        op.batch_alter_table("mail", table_kwargs={"sqlite_autoincrement": True}) as batch,
    ):
        batch.drop_constraint("fk_mail_delegation_id", type_="foreignkey")
        batch.drop_column("delegation_id")

    op.drop_index("uq_session_holds_thread_call_id", table_name="session_holds")
    conn.execute(
        sa.text(
            "UPDATE session_holds SET holder = coalesce("
            "(SELECT t.holder FROM thread_calls t WHERE t.id = session_holds.thread_call_id), "
            "'delegation:' || session_id || ':' || 'hold' || id) WHERE holder IS NULL"
        )
    )
    with op.batch_alter_table("session_holds") as batch:
        batch.drop_constraint("fk_session_holds_thread_call_id", type_="foreignkey")
        batch.drop_column("thread_call_id")
        batch.alter_column("holder", existing_type=sa.String(length=200), nullable=False)

    conn.execute(
        sa.text(
            "UPDATE tasks SET payload = json_object('through_mail_id', "
            "coalesce((SELECT max(m.id) FROM mail m WHERE m.taken_by = tasks.id), 0)) WHERE kind = 'run'"
        )
    )
    conn.execute(sa.text("UPDATE mail SET taken_by = NULL WHERE state != 'consumed'"))
    op.drop_index("ix_mail_taken_by", table_name="mail")
    op.execute("ALTER TABLE mail RENAME COLUMN taken_by TO consumed_by")
    op.create_index(
        "ix_mail_consumed_by", "mail", ["consumed_by"], sqlite_where=sa.text("consumed_by IS NOT NULL")
    )
