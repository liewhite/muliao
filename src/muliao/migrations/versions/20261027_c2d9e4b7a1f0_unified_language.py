"""unified language: actor, memory agent_id, delegation parent

统一语言（#246），只改列名与一处取值：
- issue 时间线与创建者里用户统一记为 'user'（定义在 agents/ids.py 的 USER）：issue_activities.actor_agent_id → actor、
  issues.creator_agent_id → creator，原来的 NULL（用户）换成 'user'，两列改为 NOT NULL（重建这两张表，保住
  sqlite_sequence：issue 编号与动态 id 永不复用）。
- memories.owner → agent_id（owner 只指条目归属与 issue 负责项目；NULL 仍是全局记忆）。
- revisions.author → actor（改的人统一叫 actor）。
- delegations.session_id → parent_session_id（委派的发起会话统一叫 parent），索引随之改名。

改名用 SQLite 原生 RENAME COLUMN（3.25+），外键、唯一约束随列改名，不重建表。降级把 'user' 换回 NULL。

Revision ID: c2d9e4b7a1f0
Revises: 101643b15832
Create Date: 2026-10-27 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from muliao.platform.migration_ops import keep_sequence

revision: str = "c2d9e4b7a1f0"
down_revision: str | None = "101643b15832"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAMES = (
    ("memories", "owner", "agent_id"),
    ("revisions", "author", "actor"),
    ("delegations", "session_id", "parent_session_id"),
)


def upgrade() -> None:
    op.execute("UPDATE issues SET creator_agent_id = 'user' WHERE creator_agent_id IS NULL")
    op.execute("UPDATE issue_activities SET actor_agent_id = 'user' WHERE actor_agent_id IS NULL")
    _issue_columns(("creator_agent_id", "creator"), ("actor_agent_id", "actor"), nullable=False)
    for table, old, new in _RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    op.drop_index("ix_delegations_session_id", table_name="delegations")
    op.create_index("ix_delegations_parent_session_id", "delegations", ["parent_session_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_delegations_parent_session_id", table_name="delegations")
    op.create_index("ix_delegations_session_id", "delegations", ["parent_session_id"], unique=False)
    for table, old, new in _RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN {new} TO {old}")
    _issue_columns(("creator", "creator_agent_id"), ("actor", "actor_agent_id"), nullable=True)
    op.execute("UPDATE issues SET creator_agent_id = NULL WHERE creator_agent_id = 'user'")
    op.execute("UPDATE issue_activities SET actor_agent_id = NULL WHERE actor_agent_id = 'user'")


def _issue_columns(creator: tuple[str, str], actor: tuple[str, str], *, nullable: bool) -> None:
    """改名并改可空性要重建表：带上 AUTOINCREMENT 并保住 sqlite_sequence。"""
    for table, (old, new) in (("issues", creator), ("issue_activities", actor)):
        with keep_sequence(table):
            with op.batch_alter_table(table, table_kwargs={"sqlite_autoincrement": True}) as batch_op:
                batch_op.alter_column(
                    old, new_column_name=new, existing_type=sa.String(length=32), nullable=nullable
                )
