"""记忆正文规整空白（#361）：每行去掉行尾空白、连续空行合并成一个、去掉首尾空行与结尾换行（与 `agents.memory_file.tidy` 同一
规则，这里抄一份：迁移不引用会变的应用代码）。之后每次写入都由 MemoryStore 规整，这次把已有的对齐，存下的就是指令里显示
的样子。

数据：只改 items 里 kind = memory 且规整后有变化的行的 content 与 updated_at，每块记一版（revisions，actor `system`）。
降级：不动数据（只差空白，旧代码照样能用）。

Revision ID: a8f1c2d3e4b5
Revises: 7c3e5a9d2b41
Create Date: 2026-11-04 15:50:00.000000
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "a8f1c2d3e4b5"
down_revision: str | None = "7c3e5a9d2b41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tidy(content: str) -> str:
    lines: list[str] = []
    for line in content.splitlines():
        line = line.rstrip()
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    for name, content in conn.execute(sa.text("SELECT name, content FROM items WHERE kind = 'memory'")).all():
        tidied = _tidy(content)
        if tidied == content:
            continue
        conn.execute(
            sa.text("UPDATE items SET content = :c, updated_at = :t WHERE kind = 'memory' AND name = :n"),
            {"c": tidied, "t": now, "n": name},
        )
        conn.execute(
            sa.text(
                "INSERT INTO revisions (subject, name, before, after, actor, message, created_at) "
                "VALUES ('memory', :n, :b, :a, 'system', 'tidy whitespace (#361)', :t)"
            ),
            {"n": name, "b": content, "a": tidied, "t": now},
        )


def downgrade() -> None:
    pass
