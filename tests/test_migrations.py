import sqlite3

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

import muliao.db
from muliao.db import Base, Database, alembic_config, create_engine, migrate


def _tables(path) -> set[str]:  # noqa: ANN001
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


async def test_migrated_schema_matches_models(tmp_path):
    """改了模型却没写迁移时失败：运行 `uv run alembic revision --autogenerate` 补上。"""
    migrate(tmp_path / "m.db", tmp_path / "agents")
    engine = create_engine(tmp_path / "m.db")

    def diff(conn):  # noqa: ANN001, ANN202
        ctx = MigrationContext.configure(conn, opts={"render_as_batch": True})
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []
    await engine.dispose()


def test_migrate_is_idempotent_and_reaches_head(tmp_path):
    path = tmp_path / "m.db"
    migrate(path, tmp_path / "agents")
    migrate(path, tmp_path / "agents")

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with sqlite3.connect(path) as conn:
        assert conn.execute("select version_num from alembic_version").fetchone()[0] == head


async def test_foreign_keys_are_enforced_after_migration(tmp_path):
    migrate(tmp_path / "m.db", tmp_path / "agents")
    engine = create_engine(tmp_path / "m.db")
    async with Database(engine).transaction() as s:
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
    await engine.dispose()


def test_failed_migration_rolls_back_ddl_completely(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    migrate(path, tmp_path / "agents")
    before = _tables(path)

    def broken_upgrade(cfg, _revision):  # noqa: ANN001
        conn = cfg.attributes["connection"]
        conn.exec_driver_sql("CREATE TABLE half_done (x INTEGER)")
        conn.exec_driver_sql("DROP TABLE webhooks")
        raise RuntimeError("migration bug")

    monkeypatch.setattr(muliao.db.command, "upgrade", broken_upgrade)
    with pytest.raises(RuntimeError, match="migration bug"):
        migrate(path, tmp_path / "agents")

    assert _tables(path) == before


def test_foreign_key_violation_rolls_back_migration(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    migrate(path, tmp_path / "agents")

    def orphaning_upgrade(cfg, _revision):  # noqa: ANN001
        cfg.attributes["connection"].exec_driver_sql(
            "INSERT INTO sessions (id, agent_id, title, origin, delivery, unread, created_at, updated_at) "
            "VALUES ('s', 'ghost', 't', 'user', 'none', 0, '2026-01-01', '2026-01-01')"
        )

    monkeypatch.setattr(muliao.db.command, "upgrade", orphaning_upgrade)
    with pytest.raises(RuntimeError, match="foreign key violations"):
        migrate(path, tmp_path / "agents")

    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from sessions").fetchone()[0] == 0


BEFORE_AGENT_IDS = "c6cd52aaab8c"


def _seed_name_keyed(path, agents_dir):  # noqa: ANN001
    """按 agent 改用 id 之前的结构写入数据和目录。"""
    migrate(path, agents_dir, target=BEFORE_AGENT_IDS)
    ts = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        for name in ("研究助理", "alpha"):
            conn.execute("INSERT INTO agents VALUES (?, 'd', 'm:x', '', 5, 0, 0, ?, ?)", (name, ts, ts))
            (agents_dir / name / "workspace").mkdir(parents=True)
            (agents_dir / name / "memory.md").write_text(f"memory of {name}")
        conn.execute(
            "INSERT INTO sessions VALUES ('s1', '研究助理', 't', 'user', NULL, 'none', NULL, 0, ?, ?)",
            (ts, ts),
        )
        conn.execute(
            "INSERT INTO tasks (kind, lane, agent_name, session_id, payload, status, attempts, not_before, created_at)"
            " VALUES ('reflection', 'reflection:研究助理', '研究助理', NULL, '{}', 'pending', 0, ?, ?)",
            (ts, ts),
        )
        conn.execute(
            "INSERT INTO mcp_servers (owner, name, transport, args, env, headers, enabled, created_at)"
            " VALUES ('alpha', 'echo', 'stdio', '[]', '{}', '{}', 1, ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO mcp_servers (owner, name, transport, args, env, headers, enabled, created_at)"
            " VALUES (NULL, 'shared', 'http', '[]', '{}', '{}', 1, ?)",
            (ts,),
        )


def test_agent_id_migration_rekeys_rows_lanes_and_directories(tmp_path):
    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    _seed_name_keyed(path, agents_dir)

    migrate(path, agents_dir)

    with sqlite3.connect(path) as conn:
        ids = dict(conn.execute("SELECT name, id FROM agents"))
        assert set(ids) == {"研究助理", "alpha"} and all(len(i) == 12 for i in ids.values())
        assert conn.execute("SELECT agent_id FROM sessions").fetchone()[0] == ids["研究助理"]
        assert conn.execute("SELECT agent_id, lane FROM tasks").fetchone() == (
            ids["研究助理"],
            f"reflection:{ids['研究助理']}",
        )
        assert dict(conn.execute("SELECT name, owner FROM mcp_servers")) == {
            "echo": ids["alpha"],
            "shared": None,
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for name, agent_id in ids.items():
        assert not (agents_dir / name).exists()
        assert (agents_dir / agent_id / "memory.md").read_text() == f"memory of {name}"


def test_agent_id_migration_rolls_back_when_directories_cannot_move(tmp_path):
    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    _seed_name_keyed(path, agents_dir)
    blocker = agents_dir / "alpha"
    (blocker / "memory.md").chmod(0o444)
    agents_dir.chmod(0o555)  # 目录不可写：改名失败
    try:
        with pytest.raises(OSError):
            migrate(path, agents_dir)
    finally:
        agents_dir.chmod(0o755)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == BEFORE_AGENT_IDS
        assert {r[0] for r in conn.execute("SELECT name FROM agents")} == {"研究助理", "alpha"}
    assert (agents_dir / "研究助理" / "memory.md").exists() and (agents_dir / "alpha" / "memory.md").exists()
