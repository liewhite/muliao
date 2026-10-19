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

import logging
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op

revision: str = "5e1d7c3a9b20"
down_revision: str | None = "c6cd52aaab8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger(__name__)

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
    # 有意冻结的生成规则副本（与 muliao.ids.new_agent_id 相同）：迁移不随应用代码变化
    while True:
        candidate = uuid.uuid4().hex[:12]
        if candidate not in taken:
            taken.add(candidate)
            return candidate


def _move_dirs(agents_dir: Path, mapping: dict[str, str]) -> None:
    """agents/<名称> → agents/<id>。每个 agent 都必须有目录（AgentService.create 总会创建），缺失即异常。

    在数据库提交之前执行；若目录已移动而随后提交失败，数据库回到按名称的结构、目录却已是 id，
    需要按日志里的映射手工把目录改回名称后再启动。
    """
    moves = [(agents_dir / name, agents_dir / agent_id) for name, agent_id in mapping.items()]
    missing = [str(src) for src, _ in moves if not src.is_dir()]
    if missing:
        raise RuntimeError(f"agent directories are missing, refusing to migrate: {missing}")
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
    log.info("moved agent directories", extra={"moves": {str(s): str(d) for s, d in done}})


def _orphans(conn: sa.Connection) -> list[str]:
    """可空的引用列在旧库中指向不存在的 agent：映射会得到 NULL，私有 MCP 会悄悄变成公共的，必须拒绝。"""
    rows = conn.exec_driver_sql(
        "SELECT 'mcp_servers', id FROM mcp_servers WHERE owner IS NOT NULL "
        "AND owner NOT IN (SELECT name FROM agents) UNION ALL "
        "SELECT 'tasks', id FROM tasks WHERE agent_name IS NOT NULL AND agent_name NOT IN (SELECT name FROM agents)"
    ).fetchall()
    return [f"{table}#{row_id}" for table, row_id in rows]


def upgrade() -> None:
    conn = op.get_bind()
    if orphans := _orphans(conn):
        raise RuntimeError(f"rows reference agents that do not exist, refusing to migrate: {orphans[:20]}")
    names = [row[0] for row in conn.exec_driver_sql("SELECT name FROM agents")]
    taken: set[str] = set()
    mapping = {name: _new_id(taken) for name in names}
    task_seq = conn.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = 'tasks'").scalar()

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

    # 重建表会让 AUTOINCREMENT 计数器只记得现存最大 id；写回历史最大值，保持 id 单调不复用
    if task_seq is not None:
        current = conn.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = 'tasks'").scalar()
        if current is None:  # 重建后的表为空时 sqlite_sequence 里没有这一行
            conn.execute(
                sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('tasks', :seq)"), {"seq": task_seq}
            )
        else:
            conn.execute(
                sa.text("UPDATE sqlite_sequence SET seq = :seq WHERE name = 'tasks'"),
                {"seq": max(current, task_seq)},
            )

    # lane 中嵌着 agent 标识：从已映射好的 agent_id 列推导（与 runtime.tasks 的 lane 规则一致），不按名称字符串匹配
    op.execute("UPDATE tasks SET lane = 'reflection:' || agent_id WHERE kind = 'reflection'")
    op.execute(
        "UPDATE tasks SET lane = 'deliver:' || agent_id "
        "WHERE kind = 'deliver' AND session_id IS NULL AND agent_id IS NOT NULL"
    )

    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"agent id migration left foreign key violations: {violations[:10]}")

    log.info("agent ids assigned", extra={"name_to_id": mapping})
    if mapping:
        agents_dir = context.config.attributes.get("agents_dir")
        if agents_dir is None:
            raise RuntimeError("agent id migration needs the agents data directory to move agent folders")
        _move_dirs(Path(agents_dir), mapping)


def downgrade() -> None:
    raise NotImplementedError("agent id migration is irreversible")
