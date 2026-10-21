import sqlite3

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

import muliao.platform.db
from muliao.platform.db import Base, Database, alembic_config, create_engine, migrate


def _tables(path) -> set[str]:  # noqa: ANN001
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


async def test_migrated_schema_matches_models(tmp_path):
    """改了模型却没写迁移时失败：运行 `uv run alembic revision --autogenerate` 补上。"""
    migrate(tmp_path / "m.db")
    engine = create_engine(tmp_path / "m.db")

    def diff(conn):  # noqa: ANN001, ANN202
        ctx = MigrationContext.configure(conn, opts={"render_as_batch": True})
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []
    await engine.dispose()


def test_migrate_is_idempotent_and_reaches_head(tmp_path):
    path = tmp_path / "m.db"
    migrate(path)
    migrate(path)

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with sqlite3.connect(path) as conn:
        assert conn.execute("select version_num from alembic_version").fetchone()[0] == head


async def test_foreign_keys_are_enforced_after_migration(tmp_path):
    migrate(tmp_path / "m.db")
    engine = create_engine(tmp_path / "m.db")
    async with Database(engine).transaction() as s:
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
    await engine.dispose()


def test_failed_migration_rolls_back_ddl_completely(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    migrate(path)
    before = _tables(path)

    def broken_upgrade(cfg, _revision):  # noqa: ANN001
        conn = cfg.attributes["connection"]
        conn.exec_driver_sql("CREATE TABLE half_done (x INTEGER)")
        conn.exec_driver_sql("DROP TABLE compactions")
        raise RuntimeError("migration bug")

    monkeypatch.setattr(muliao.platform.db.command, "upgrade", broken_upgrade)
    with pytest.raises(RuntimeError, match="migration bug"):
        migrate(path)

    assert _tables(path) == before


def test_foreign_key_violation_rolls_back_migration(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    migrate(path)

    def orphaning_upgrade(cfg, _revision):  # noqa: ANN001
        cfg.attributes["connection"].exec_driver_sql(
            "INSERT INTO messages (session_id, task_id, payload, created_at) VALUES ('ghost', 1, '{}', '2026-01-01')"
        )

    monkeypatch.setattr(muliao.platform.db.command, "upgrade", orphaning_upgrade)
    with pytest.raises(RuntimeError, match="foreign key violations"):
        migrate(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from messages").fetchone()[0] == 0


def _insert_trigger(conn: sqlite3.Connection, name: str, agent_id: str = "a1a1a1a1a1a1") -> None:
    conn.execute(
        "insert into triggers (name, agent_id, prompt, kind, config, state, enabled, created_at) "
        "values (?, ?, 'p', 'schedule', '{\"cron\": \"* * * * *\"}', '{}', 1, '2026-01-01')",
        (name, agent_id),
    )


def test_existing_triggers_survive_the_finished_at_upgrade_and_only_then_share_names(tmp_path):
    """已有库升级：触发器原样保留、finished_at 为 NULL，名称改为只在未归档的之间唯一。"""
    path = tmp_path / "m.db"
    migrate(path, "cd85203e73a8")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, tool_groups, reflect_every, created_at, "
            "updated_at) values ('a1a1a1a1a1a1', 'alpha', '', 'm', '[]', 0, '2026-01-01', '2026-01-01')"
        )
        _insert_trigger(conn, "daily")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_trigger(conn, "daily")

    migrate(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("select name, finished_at from triggers").fetchall() == [("daily", None)]
        # 未归档的之间仍然唯一
        with pytest.raises(sqlite3.IntegrityError):
            _insert_trigger(conn, "daily")
        conn.execute("update triggers set finished_at = '2026-01-02' where name = 'daily'")
        _insert_trigger(conn, "daily")
        assert conn.execute("select count(*) from triggers where name = 'daily'").fetchone()[0] == 2
