"""mail origin：信件记下发送方来源；messages.created_at 加索引（#368）

经来信入口投的信件（消息、用户对提问的回答）记下发送方的来源（mail.origin，sessions.Origin 的值），「用户说了什么」
只按它认（sessions.recall 的热历史工具）；事件与交回的结局为 NULL。messages.created_at 加索引：按日查各会话的活动。

回填旧数据（会改数据）：迁移前没记发送方，按当时的投信规则推出（别的来源只能续写自己发起的会话，用户能写任何会话）：
- 消息：用户开的会话里都是 user；issue 会话里以「New activity on issue」开头的是 issue，其余 user；触发器会话里
  第一封消息是 trigger，其余 user；thread 里带关联 id 的、或与父会话某次 delegate_task / ask_agent 交出的文字相同的
  （父会话已删除时无从比对，也算）是 delegation，其余 user。
- 回答：内容是 ask_user 的结果的（只有用户在界面上回答提问会投这种回答）是 user，其余（ask_agent 交回的）NULL。

降级删列与索引（SQLite 原生 DROP COLUMN，不重建表，mail 的 AUTOINCREMENT 与 sqlite_sequence 不受影响）。

Revision ID: cb9354fa36e7
Revises: a8f1c2d3e4b5
Create Date: 2026-11-04 17:16:40.102109
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cb9354fa36e7"
down_revision: str | None = "a8f1c2d3e4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 迁移时的文字与工具参数，迁移里自带一份，不随代码变
_ISSUE_PREFIX = "New activity on issue"
_DELEGATION_ARGS = {"delegate_task": "task", "ask_agent": "message"}


def _delegated_texts(bind: sa.Connection, parent_id: str) -> set[str]:
    """父会话用 delegate_task / ask_agent 交出去的文字。"""
    texts = set()
    rows = bind.execute(
        sa.text(
            "SELECT payload FROM messages WHERE session_id = :p "
            "AND (payload LIKE '%delegate_task%' OR payload LIKE '%ask_agent%')"
        ),
        {"p": parent_id},
    ).all()
    for (payload,) in rows:
        for part in json.loads(payload)["parts"]:
            key = (
                _DELEGATION_ARGS.get(part.get("tool_name", "")) if part["part_kind"] == "tool-call" else None
            )
            if key is None:
                continue
            args = part["args"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            if isinstance(args, dict) and isinstance(args.get(key), str):
                texts.add(args[key])
    return texts


def _message_origin(
    bind: sa.Connection, row: sa.Row, first_of_session: dict[str, int], parents: dict[str, set[str]]
) -> str:
    origin = row.session_origin
    if origin == "user":
        return "user"
    if origin == "issue":
        return "issue" if row.content.startswith(_ISSUE_PREFIX) else "user"
    if origin == "trigger":
        return "trigger" if first_of_session[row.session_id] == row.id else "user"
    if origin == "delegation":
        if row.correlation is not None or row.parent_session_id is None:
            return "delegation"
        if row.parent_session_id not in parents:
            parents[row.parent_session_id] = _delegated_texts(bind, row.parent_session_id)
        return "delegation" if row.content in parents[row.parent_session_id] else "user"
    raise RuntimeError(f"session {row.session_id} has an unknown origin {origin!r}")


def upgrade() -> None:
    op.add_column("mail", sa.Column("origin", sa.String(length=16), nullable=True))
    op.create_index("ix_messages_created_at", "messages", ["created_at"], unique=False)
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT m.id, m.session_id, m.kind, m.content, m.answers, m.correlation, "
            "s.origin AS session_origin, s.parent_session_id "
            "FROM mail m JOIN sessions s ON s.id = m.session_id "
            "WHERE m.kind IN ('message', 'answer') ORDER BY m.id"
        )
    ).all()
    first_of_session: dict[str, int] = {}
    for row in rows:
        if row.kind == "message":
            first_of_session.setdefault(row.session_id, row.id)
    parents: dict[str, set[str]] = {}
    by_origin: dict[str, list[int]] = {}
    for row in rows:
        if row.kind == "message":
            origin: str | None = _message_origin(bind, row, first_of_session, parents)
        else:
            answers = json.loads(row.answers)
            origin = "user" if answers and all(a["tool"] == "ask_user" for a in answers.values()) else None
        if origin is not None:
            by_origin.setdefault(origin, []).append(row.id)
    for origin, ids in by_origin.items():
        for i in range(0, len(ids), 500):
            bind.execute(
                sa.text("UPDATE mail SET origin = :o WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"o": origin, "ids": ids[i : i + 500]},
            )


def downgrade() -> None:
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_column("mail", "origin")
