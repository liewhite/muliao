"""会话信箱：mail 表取代每条消息一轮的 turn payload 与 session_events（设计见 docs/architecture.md「会话信箱」）

数据转换：
- 还要处理的 turn task 的输入转成排队的信件：回答一封 answer，文字或附件一封 message；到达时间沿用 task 的创建
  时间。还要处理的是排队、运行中（停服时被打断）的，以及之后没有完成过别的 turn 的失败 turn——之后有完成的，旧系统
  里它只是一条失败记录，会话已经越过了它，不能把几周前的输入再交给模型。排队与运行中的 task 删除（唤醒规则会为信件
  投递运行）；失败的 task 保留，用户重试时它把信件带上——信件不晚于它，唤醒规则不会因为上线就自动重跑失败的工作。
  失败 turn 的委派当时已经交回过失败，它的信件不再带交回地址（每个委派只交回一次）。
- session_events 里没处理的转成排队的 event 信件；已处理的内容已在当时那一轮的消息里，表删除。
- jobs.reply_to 换算成 awaited（发起任务时用户是否在等回答）。
- turn task 的 payload 清空：输入都在信箱里。

不可回滚：删除的 payload 与 session_events 无法还原。

Revision ID: 19a120b722f5
Revises: a3339b22b82a
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "19a120b722f5"
down_revision: str | None = "a3339b22b82a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPORT_TO = (
    "CASE WHEN json_extract(payload, '$.report_to') IS NULL THEN NULL ELSE json_object("
    "'session_id', json_extract(payload, '$.report_to.session_id'), "
    "'awaited', json(CASE WHEN json_extract(payload, '$.report_to.reply_to') = 'user' THEN 'true' ELSE 'false' END)"
    ") END"
)


# 还要处理的 turn（见模块说明）；t 是 tasks 的别名
_TO_CONVERT = (
    "t.kind = 'turn' AND (t.status IN ('pending', 'running') OR (t.status = 'failed' AND NOT EXISTS ("
    "SELECT 1 FROM tasks later WHERE later.kind = 'turn' AND later.session_id = t.session_id "
    "AND later.status = 'done' AND later.id > t.id)))"
)


def upgrade() -> None:
    op.create_table(
        "mail",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("awaited", sa.Boolean(), nullable=False),
        sa.Column("report_to", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("consumed_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["consumed_by"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("mail", schema=None) as batch_op:
        batch_op.create_index(
            "ix_mail_queued", ["session_id"], unique=False, sqlite_where=sa.text("state = 'queued'")
        )
        batch_op.create_index(batch_op.f("ix_mail_session_id"), ["session_id"], unique=False)

    # 一条 INSERT、按到达时间排好：mail.id 就是信箱的顺序（同一个 turn 的回答在消息之前）
    op.execute(
        "INSERT INTO mail (session_id, kind, source, content, attachments, answers, awaited, report_to, state, "
        "created_at) SELECT session_id, kind, source, content, attachments, answers, awaited, report_to, 'queued', "
        "created_at FROM ("
        f"SELECT session_id, 'answer' AS kind, NULL AS source, '' AS content, '[]' AS attachments, "
        "json_extract(payload, '$.answers') AS answers, json_extract(payload, '$.reply_to') = 'user' AS awaited, "
        f"NULL AS report_to, created_at, id AS turn, 0 AS ord FROM tasks t WHERE {_TO_CONVERT} "
        "AND json_extract(payload, '$.answers') != '{}' "
        "UNION ALL "
        "SELECT session_id, 'message', NULL, json_extract(payload, '$.prompt'), json_extract(payload, '$.attachments'), "
        f"'{{}}', json_extract(payload, '$.reply_to') = 'user', CASE WHEN status = 'failed' THEN NULL ELSE {_REPORT_TO} "
        f"END, created_at, id, 1 FROM tasks t WHERE {_TO_CONVERT} "
        "AND (json_extract(payload, '$.prompt') != '' OR json_extract(payload, '$.attachments') != '[]') "
        "UNION ALL "
        "SELECT session_id, 'event', source, content, '[]', '{}', reply_to = 'user', NULL, created_at, 0, 2 "
        "FROM session_events WHERE consumed_by IS NULL"
        ") ORDER BY created_at, turn, ord"
    )
    op.execute("DELETE FROM tasks WHERE kind = 'turn' AND status IN ('pending', 'running')")
    op.execute("UPDATE tasks SET payload = '{}' WHERE kind = 'turn'")
    with op.batch_alter_table("session_events", schema=None) as batch_op:
        batch_op.drop_index("ix_session_events_queued")
        batch_op.drop_index(batch_op.f("ix_session_events_session_id"))
    op.drop_table("session_events")

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("awaited", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE jobs SET awaited = (reply_to = 'user')")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.alter_column("awaited", server_default=None)
        batch_op.drop_column("reply_to")


def downgrade() -> None:
    raise RuntimeError(
        "the session mailbox migration is irreversible: turn payloads and session_events were dropped"
    )
