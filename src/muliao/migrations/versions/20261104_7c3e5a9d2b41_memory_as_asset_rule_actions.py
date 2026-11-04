"""AI 资产统一 RBAC（#365）：规则加动作 `action:kind:name`，记忆成为资产（items.kind = memory），删 memories 表。

数据：
- roles 与 agent_rules 的 allow / deny：每条 `kind:glob` 改写成 `use:kind:glob`（原来的规则都是「能用」）；已删的工具
  `tool:memory_edit_public` 去掉（它的能力现在是 admin 固定的 `write:*`）。
- memories 的行搬进 items：全局记忆（agent_id 为 NULL）→ name `global`、owner NULL（从未写过时补一行空的）；agent 的记忆 →
  name 取 agent 当时的名字、owner 为该 agent。content、updated_at 照搬，created_at 取 updated_at。名字冲突（有 agent 叫
  global）时报错停下，不猜。
- revisions 里 subject = memory 的版本改名：空串 → `global`，现有 agent 的 id → 它的名字（已删 agent 的旧版本保留原 id）。
- 删掉 memories 表。

降级：重建 memories（与 c2d9e4b7a1f0 之后的结构一致），`global` 搬回 agent_id NULL，归某个 agent 的记忆（每个 agent 取 id
最小的一块）搬回它的行，其余记忆（共享记忆）丢弃；版本名改回；规则去掉 `use:` 前缀，带 write / `*` 动作或针对记忆的
规则去掉。

Revision ID: 7c3e5a9d2b41
Revises: 1b161820af45
Create Date: 2026-11-04 14:30:00.000000
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "7c3e5a9d2b41"
down_revision: str | None = "1b161820af45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GLOBAL = "global"
_DROPPED_TOOL_RULES = frozenset({"tool:memory_edit_public"})


def _rewrite_rules(convert) -> None:
    conn = op.get_bind()
    for table, key in (("roles", "name"), ("agent_rules", "agent_id")):
        for row in conn.execute(sa.text(f"SELECT {key}, allow, deny FROM {table}")).all():
            allow = [r for p in json.loads(row[1]) if (r := convert(p)) is not None]
            deny = [r for p in json.loads(row[2]) if (r := convert(p)) is not None]
            conn.execute(
                sa.text(f"UPDATE {table} SET allow = :a, deny = :d WHERE {key} = :k"),
                {
                    "a": json.dumps(allow, ensure_ascii=False),
                    "d": json.dumps(deny, ensure_ascii=False),
                    "k": row[0],
                },
            )


def _up_rule(pattern: str) -> str | None:
    return None if pattern in _DROPPED_TOOL_RULES else f"use:{pattern}"


def _down_rule(pattern: str) -> str | None:
    action, _, rest = pattern.partition(":")
    if action != "use" or rest == "*" or rest.startswith("memory:"):
        return None
    return rest


def upgrade() -> None:
    conn = op.get_bind()
    _rewrite_rules(_up_rule)

    names = dict(conn.execute(sa.text("SELECT id, name FROM agents")).all())
    if _GLOBAL in names.values():
        raise RuntimeError(f"an agent is named {_GLOBAL!r}, the name of the global memory; rename it first")
    rows = [tuple(r) for r in conn.execute(sa.text("SELECT agent_id, content, updated_at FROM memories"))]
    if not any(r[0] is None for r in rows):
        # 与 platform.db.UTCDateTime 存的形式一致（UTC、无时区）
        rows.append((None, "", datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")))
    for agent_id, content, updated_at in rows:
        conn.execute(
            sa.text(
                "INSERT INTO items (kind, name, owner, description, content, enabled, created_at, updated_at) "
                "VALUES ('memory', :n, :o, '', :c, 1, :t, :t)"
            ),
            {
                "n": _GLOBAL if agent_id is None else names[agent_id],
                "o": agent_id,
                "c": content,
                "t": updated_at,
            },
        )
    conn.execute(
        sa.text("UPDATE revisions SET name = :g WHERE subject = 'memory' AND name = ''"), {"g": _GLOBAL}
    )
    for agent_id, name in names.items():
        conn.execute(
            sa.text("UPDATE revisions SET name = :n WHERE subject = 'memory' AND name = :i"),
            {"n": name, "i": agent_id},
        )
    op.drop_table("memories")


def downgrade() -> None:
    conn = op.get_bind()
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id"),
        sqlite_autoincrement=True,
    )
    rows = conn.execute(
        sa.text("SELECT name, owner, content, updated_at FROM items WHERE kind = 'memory' ORDER BY id")
    ).all()
    seen: set[str | None] = set()
    for name, owner, content, updated_at in rows:
        agent_id = None if name == _GLOBAL and owner is None else owner
        if (owner is None and name != _GLOBAL) or agent_id in seen:
            continue
        seen.add(agent_id)
        conn.execute(
            sa.text("INSERT INTO memories (agent_id, content, updated_at) VALUES (:a, :c, :t)"),
            {"a": agent_id, "c": content, "t": updated_at},
        )
        conn.execute(
            sa.text("UPDATE revisions SET name = :old WHERE subject = 'memory' AND name = :n"),
            {"old": agent_id or "", "n": name},
        )
    conn.execute(sa.text("DELETE FROM items WHERE kind = 'memory'"))
    _rewrite_rules(_down_rule)
