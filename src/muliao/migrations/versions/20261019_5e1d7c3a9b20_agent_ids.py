"""agent 改用系统生成的 id 作标识；名称只用于展示

Revision ID: 5e1d7c3a9b20
Revises: c6cd52aaab8c
Create Date: 2026-10-19 16:20:00

- agents：主键由 name 改为 id（12 位小写十六进制），name 保留为唯一的展示名。
- sessions / tasks / reflections / cron_jobs / webhooks：agent_name 列换成 agent_id；mcp_servers.owner 的值由名称换成 id。
- tasks.lane 中嵌入的 agent 名称（reflection:<名称>、无 session 的 deliver:<名称>）换成 id。
- 数据目录 agents/<名称> 改名为 agents/<id>。目录改名不在数据库事务内：放在所有数据库变更与外键检查之后，
  改名前预检，改名中途失败会把已改的目录改回并抛错，数据库随之整体回滚。

不可降级：降级需要把 id 映射回名称并移回目录，没有实际需要，因此直接报错。
"""

import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op

revision: str = "5e1d7c3a9b20"
down_revision: str | None = "c6cd52aaab8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与旧表列一一对应的新表定义；{agent} 处由 agent_name 的映射结果填入 agent_id
_REBUILDS: dict[str, tuple[str, str, list[str]]] = {
    "sessions": (
        """CREATE TABLE sessions_new (
            id VARCHAR(36) NOT NULL, agent_id VARCHAR(32) NOT NULL, title VARCHAR(200) NOT NULL,
            origin VARCHAR(16) NOT NULL, origin_ref VARCHAR(64), delivery VARCHAR(16) NOT NULL,
            delivery_target VARCHAR(64), unread BOOLEAN NOT NULL, created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id), FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE)""",
        "id, {agent}, title, origin, origin_ref, delivery, delivery_target, unread, created_at, updated_at",
        [
            "CREATE INDEX ix_sessions_origin_ref ON sessions (origin, origin_ref)",
            "CREATE INDEX ix_sessions_agent_id ON sessions (agent_id)",
            "CREATE INDEX ix_sessions_updated_at ON sessions (updated_at)",
        ],
    ),
    "tasks": (
        """CREATE TABLE tasks_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, kind VARCHAR(16) NOT NULL, lane VARCHAR(100) NOT NULL,
            agent_id VARCHAR(32), session_id VARCHAR(36), payload JSON NOT NULL, status VARCHAR(16) NOT NULL,
            attempts INTEGER NOT NULL, error TEXT, not_before DATETIME NOT NULL, created_at DATETIME NOT NULL,
            started_at DATETIME, finished_at DATETIME,
            FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE)""",
        "id, kind, lane, {agent}, session_id, payload, status, attempts, error, not_before, created_at, "
        "started_at, finished_at",
        [
            "CREATE INDEX ix_tasks_status_lane ON tasks (status, lane)",
            "CREATE INDEX ix_tasks_agent_id ON tasks (agent_id)",
            "CREATE INDEX ix_tasks_session_id ON tasks (session_id)",
        ],
    ),
    "reflections": (
        """CREATE TABLE reflections_new (
            id INTEGER NOT NULL, agent_id VARCHAR(32) NOT NULL, from_message_id INTEGER NOT NULL,
            to_message_id INTEGER NOT NULL, output TEXT NOT NULL, messages TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id), FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE)""",
        "id, {agent}, from_message_id, to_message_id, output, messages, created_at",
        ["CREATE INDEX ix_reflections_agent_id ON reflections (agent_id)"],
    ),
    "cron_jobs": (
        """CREATE TABLE cron_jobs_new (
            id INTEGER NOT NULL, name VARCHAR(100) NOT NULL, agent_id VARCHAR(32) NOT NULL,
            schedule VARCHAR(100) NOT NULL, prompt TEXT NOT NULL, enabled BOOLEAN NOT NULL,
            next_run_at DATETIME NOT NULL, last_run_at DATETIME, last_session_id VARCHAR(36), last_error TEXT,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id), UNIQUE (name), FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE)""",
        "id, name, {agent}, schedule, prompt, enabled, next_run_at, last_run_at, last_session_id, last_error, "
        "created_at",
        [],
    ),
    "webhooks": (
        """CREATE TABLE webhooks_new (
            id VARCHAR(36) NOT NULL, name VARCHAR(100) NOT NULL, agent_id VARCHAR(32) NOT NULL,
            token VARCHAR(64) NOT NULL, prompt TEXT NOT NULL, created_at DATETIME NOT NULL,
            PRIMARY KEY (id), UNIQUE (name), FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE)""",
        "id, name, {agent}, token, prompt, created_at",
        [],
    ),
}

_AGENT_ID = "(SELECT agents_new.id FROM agents_new WHERE agents_new.name = {table}.agent_name)"


def _new_id(taken: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:12]
        if candidate not in taken:
            taken.add(candidate)
            return candidate


def _move_dirs(agents_dir: Path, mapping: dict[str, str]) -> None:
    moves = [(agents_dir / name, agents_dir / agent_id) for name, agent_id in mapping.items()]
    moves = [(src, dst) for src, dst in moves if src.exists()]
    clashes = [str(dst) for _, dst in moves if dst.exists()]
    if clashes:
        raise RuntimeError(f"cannot move agent directories, targets already exist: {clashes}")
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            src.rename(dst)
            done.append((src, dst))
    except Exception:
        for src, dst in reversed(done):
            dst.rename(src)
        raise


def upgrade() -> None:
    conn = op.get_bind()
    names = [row[0] for row in conn.exec_driver_sql("SELECT name FROM agents")]
    taken: set[str] = set()
    mapping = {name: _new_id(taken) for name in names}

    op.execute(
        """CREATE TABLE agents_new (
            id VARCHAR(32) NOT NULL, name VARCHAR(64) NOT NULL, description TEXT NOT NULL,
            model VARCHAR(200) NOT NULL, instructions TEXT NOT NULL, reflect_every INTEGER NOT NULL,
            turns_since_reflection INTEGER NOT NULL, reflection_checkpoint INTEGER NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            PRIMARY KEY (id), UNIQUE (name))"""
    )
    for name, agent_id in mapping.items():
        conn.execute(
            sa.text(
                "INSERT INTO agents_new SELECT :id, name, description, model, instructions, reflect_every, "
                "turns_since_reflection, reflection_checkpoint, created_at, updated_at FROM agents WHERE name = :name"
            ),
            {"id": agent_id, "name": name},
        )

    for table, (ddl, columns, _indexes) in _REBUILDS.items():
        op.execute(ddl)
        select_cols = columns.format(agent=_AGENT_ID.format(table=table))
        op.execute(
            f"INSERT INTO {table}_new ({columns.format(agent='agent_id')}) SELECT {select_cols} FROM {table}"
        )

    op.execute(
        """CREATE TABLE mcp_servers_new (
            id INTEGER NOT NULL, owner VARCHAR(32), name VARCHAR(64) NOT NULL, transport VARCHAR(16) NOT NULL,
            command TEXT, args JSON NOT NULL, url TEXT, env JSON NOT NULL, headers JSON NOT NULL,
            enabled BOOLEAN NOT NULL, created_at DATETIME NOT NULL,
            PRIMARY KEY (id), FOREIGN KEY(owner) REFERENCES agents (id) ON DELETE CASCADE)"""
    )
    op.execute(
        "INSERT INTO mcp_servers_new SELECT id, "
        "CASE WHEN owner IS NULL THEN NULL ELSE "
        "(SELECT agents_new.id FROM agents_new WHERE agents_new.name = mcp_servers.owner) END, "
        "name, transport, command, args, url, env, headers, enabled, created_at FROM mcp_servers"
    )

    # 旧表引用旧 agents，先删子表再删父表；外键在迁移连接上已关闭
    for table in [*_REBUILDS, "mcp_servers", "agents"]:
        op.execute(f"DROP TABLE {table}")
    for table in ["agents", *_REBUILDS, "mcp_servers"]:
        op.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    for _, _, indexes in _REBUILDS.values():
        for index in indexes:
            op.execute(index)
    op.execute("CREATE UNIQUE INDEX uq_mcp_public ON mcp_servers (name) WHERE owner IS NULL")
    op.execute("CREATE UNIQUE INDEX uq_mcp_private ON mcp_servers (owner, name) WHERE owner IS NOT NULL")

    for name, agent_id in mapping.items():
        conn.execute(
            sa.text("UPDATE tasks SET lane = :new WHERE lane = :old"),
            {"new": f"reflection:{agent_id}", "old": f"reflection:{name}"},
        )
        conn.execute(
            sa.text("UPDATE tasks SET lane = :new WHERE lane = :old AND session_id IS NULL"),
            {"new": f"deliver:{agent_id}", "old": f"deliver:{name}"},
        )

    # 任何一行引用不到 agent 都说明映射出错：在动目录之前失败，整体回滚
    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"agent id migration left foreign key violations: {violations[:10]}")
    missing = conn.exec_driver_sql(
        "SELECT 'sessions' FROM sessions WHERE agent_id IS NULL UNION ALL "
        "SELECT 'reflections' FROM reflections WHERE agent_id IS NULL UNION ALL "
        "SELECT 'cron_jobs' FROM cron_jobs WHERE agent_id IS NULL UNION ALL "
        "SELECT 'webhooks' FROM webhooks WHERE agent_id IS NULL"
    ).fetchall()
    if missing:
        raise RuntimeError(f"rows reference agents that do not exist: {sorted({r[0] for r in missing})}")

    if mapping:
        agents_dir = context.config.attributes.get("agents_dir")
        if agents_dir is None:
            raise RuntimeError("agent id migration needs the agents data directory to move agent folders")
        _move_dirs(Path(agents_dir), mapping)


def downgrade() -> None:
    raise NotImplementedError("agent id migration is irreversible")
