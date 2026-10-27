"""message texts for session search (#254)

新表 message_texts：每条消息可按关键字搜到的文字（sessions.history.searchable_text），会话列表按关键字筛用。
不改已有数据，只从 messages 回填：规则与 searchable_text 相同，这里按消息 JSON 冻结一份（迁移不引用会变的应用代码）：
请求里除运行上下文（metadata.muliao_run_context 指的那段）外的用户消息文字，回复里的文字与工具调用（工具名、参数的
各个字符串值；参数不是合法 JSON 时用原文）。降级删表。

Revision ID: 20676599edfd
Revises: 29411a27d5ec
Create Date: 2026-10-27 16:47:05.475324
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20676599edfd"
down_revision: str | None = "29411a27d5ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 2000


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _args(args: object) -> object:
    if not isinstance(args, str):
        return args
    try:
        value = json.loads(args)
    except ValueError:
        return args
    return value if isinstance(value, dict) else args


def searchable_text(payload: str) -> str:
    message = json.loads(payload)
    chunks: list[str] = []
    if message["kind"] == "request":
        context = (message.get("metadata") or {}).get("muliao_run_context")
        for i, part in enumerate(message["parts"]):
            if part["part_kind"] == "user-prompt" and i != context:
                content = part["content"]
                chunks += (
                    [content] if isinstance(content, str) else [x for x in content if isinstance(x, str)]
                )
    else:
        for part in message["parts"]:
            if part["part_kind"] == "text":
                chunks.append(part["content"])
            elif part["part_kind"] == "tool-call":
                chunks += [part["tool_name"], *_strings(_args(part["args"]))]
    return "\n".join(x for x in chunks if x)


def upgrade() -> None:
    op.create_table(
        "message_texts",
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id"),
    )
    connection = op.get_bind()
    insert = sa.text("INSERT INTO message_texts (message_id, text) VALUES (:id, :text)")
    after = 0
    while True:
        rows = connection.execute(
            sa.text("SELECT id, payload FROM messages WHERE id > :after ORDER BY id LIMIT :n"),
            {"after": after, "n": _BATCH},
        ).all()
        if not rows:
            break
        texts = [{"id": i, "text": searchable_text(p)} for i, p in rows]
        texts = [x for x in texts if x["text"]]
        if texts:
            connection.execute(insert, texts)
        after = rows[-1][0]


def downgrade() -> None:
    op.drop_table("message_texts")
