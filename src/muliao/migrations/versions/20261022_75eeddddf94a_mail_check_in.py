"""mail check_in

运行中检查显式标记（#121）：mail 加列 check_in（mailbox.post_check_in 投的信件为真），交给模型时记进那条请求的
metadata `muliao_check_ins`（conventions.RUN_CHECK_INS，RUN_EVENTS 的子集）。

回填旧数据（会改数据）：按当时检查信件的固定文字识别——来源 job:N 且以「Background job N is still running (」
开头；来源 thread:<id> 且含「Keep waiting, you will get an event with its answer; if it is no longer needed, call
stop_thread(」。mail 行置 check_in；messages 里标了 muliao_events 的请求，把其中是检查的事件段下标写进
muliao_check_ins（payload 重新序列化，内容不变）。

降级删列，并从消息 metadata 去掉 muliao_check_ins。

Revision ID: 75eeddddf94a
Revises: b97a9752ebad
Create Date: 2026-10-22 23:19:02.848443
"""

import json
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "75eeddddf94a"
down_revision: str | None = "b97a9752ebad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_CHECK = re.compile(r"Background job \d+ is still running \(")
_THREAD_CHECK = (
    "Keep waiting, you will get an event with its answer; if it is no longer needed, call stop_thread("
)
# 迁移时的事件格式（conventions.format_event），迁移里自带一份，不随代码变
_EVENT = re.compile(r'\A<event source="(?P<source>[^"]*)">\n(?P<content>.*)\n</event>\Z', re.DOTALL)


def _is_check_in(source: str, content: str) -> bool:
    if source.startswith("job:"):
        return _JOB_CHECK.match(content) is not None
    if source.startswith("thread:"):
        return _THREAD_CHECK in content
    return False


def _check_ins(payload: dict) -> list[int]:
    """一条标了事件的请求里，哪些事件段是检查。事件下标指向唯一一段（非运行上下文的）用户消息的内容列表。"""
    meta = payload["metadata"]
    context_at = meta.get("muliao_run_context")
    prompts = [
        p for i, p in enumerate(payload["parts"]) if p["part_kind"] == "user-prompt" and i != context_at
    ]
    if len(prompts) != 1 or not isinstance(prompts[0]["content"], list):
        raise RuntimeError(f"request with events has an unexpected shape: {prompts!r}"[:500])
    content = prompts[0]["content"]
    found = []
    for i in meta["muliao_events"]:
        match = _EVENT.match(content[i])
        if match is None:
            raise RuntimeError(f"event segment does not follow the event format: {content[i]!r}"[:500])
        if _is_check_in(match["source"], match["content"]):
            found.append(i)
    return found


def upgrade() -> None:
    op.add_column("mail", sa.Column("check_in", sa.Boolean(), server_default="0", nullable=False))
    bind = op.get_bind()
    mail = bind.execute(sa.text("SELECT id, source, content FROM mail WHERE kind = 'event'")).all()
    ids = [m.id for m in mail if m.source is not None and _is_check_in(m.source, m.content)]
    for i in range(0, len(ids), 500):
        bind.execute(
            sa.text("UPDATE mail SET check_in = 1 WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": ids[i : i + 500]},
        )
    rows = bind.execute(
        sa.text(
            "SELECT id, payload FROM messages "
            "WHERE json_array_length(json_extract(payload, '$.metadata.muliao_events')) > 0"
        )
    ).all()
    for row in rows:
        payload = json.loads(row.payload)
        found = _check_ins(payload)
        if found:
            payload["metadata"]["muliao_check_ins"] = found
            bind.execute(
                sa.text("UPDATE messages SET payload = :p WHERE id = :id"),
                {"p": json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "id": row.id},
            )


def downgrade() -> None:
    op.execute(
        "UPDATE messages SET payload = json_remove(payload, '$.metadata.muliao_check_ins') "
        "WHERE json_extract(payload, '$.metadata.muliao_check_ins') IS NOT NULL"
    )
    # SQLite 原生 DROP COLUMN（3.35+）：不重建表，mail 的 AUTOINCREMENT 与 sqlite_sequence 不受影响
    op.drop_column("mail", "check_in")
