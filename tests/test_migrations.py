import json
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
        conn.exec_driver_sql("DROP TABLE compactions")
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
            "INSERT INTO sessions (id, agent_id, title, origin, notify, unread, created_at, updated_at) "
            "VALUES ('s', 'ghost', 't', 'user', 0, 0, '2026-01-01', '2026-01-01')"
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


def test_agent_id_migration_keeps_task_ids_monotonic(tmp_path):
    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    _seed_name_keyed(path, agents_dir)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM tasks")  # 末尾的行被删：计数器必须记得历史最大值
        before = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'tasks'").fetchone()[0]

    migrate(path, agents_dir)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'tasks'").fetchone()[0] == before


def test_agent_id_migration_refuses_rows_pointing_at_missing_agents(tmp_path):
    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    _seed_name_keyed(path, agents_dir)
    with sqlite3.connect(path) as conn:  # sqlite3 默认不检查外键，可写入孤儿行
        conn.execute("UPDATE mcp_servers SET owner = 'ghost' WHERE name = 'echo'")

    with pytest.raises(RuntimeError, match=r"mcp_servers#"):
        migrate(path, agents_dir)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == BEFORE_AGENT_IDS


def test_agent_id_migration_refuses_missing_agent_directory(tmp_path):
    import shutil

    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    _seed_name_keyed(path, agents_dir)
    shutil.rmtree(agents_dir / "alpha")

    with pytest.raises(RuntimeError, match="directories are missing"):
        migrate(path, agents_dir)
    assert (agents_dir / "研究助理").is_dir()


def test_agent_id_migration_restores_moved_dirs_when_a_later_move_fails(tmp_path, monkeypatch):
    from pathlib import Path

    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    _seed_name_keyed(path, agents_dir)
    real_rename = Path.rename
    calls = {"n": 0}

    def flaky_rename(self, target):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk hiccup")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError, match="disk hiccup"):
        migrate(path, agents_dir)

    assert sorted(p.name for p in agents_dir.iterdir()) == sorted(["研究助理", "alpha"])
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == BEFORE_AGENT_IDS


def test_agent_id_migration_leaves_system_notification_lane_alone(tmp_path):
    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    _seed_name_keyed(path, agents_dir)
    ts = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO agents VALUES ('system', 'd', 'm:x', '', 5, 0, 0, ?, ?)", (ts, ts))
        (agents_dir / "system").mkdir()
        conn.execute(
            "INSERT INTO tasks (kind, lane, agent_name, session_id, payload, status, attempts, not_before, created_at)"
            " VALUES ('deliver', 'deliver:system', NULL, NULL, '{}', 'pending', 0, ?, ?)",
            (ts, ts),
        )

    migrate(path, agents_dir)

    with sqlite3.connect(path) as conn:
        lanes = [r[0] for r in conn.execute("SELECT lane FROM tasks WHERE kind = 'deliver'")]
    assert lanes == ["deliver:system"]


BEFORE_DROP_TRIGGERS = "5e1d7c3a9b20"


def test_drop_triggers_migration_converts_delivery_to_notify(tmp_path):
    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    migrate(path, agents_dir, target=BEFORE_DROP_TRIGGERS)
    ts = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO agents VALUES ('a1', 'n', 'd', 'm:x', '', 5, 0, 0, ?, ?)", (ts, ts))
        for sid, delivery in (("s-push", "webpush"), ("s-none", "none")):
            conn.execute(
                "INSERT INTO sessions VALUES (?, 'a1', 't', 'user', NULL, ?, NULL, 0, ?, ?)",
                (sid, delivery, ts, ts),
            )
        conn.execute(
            "INSERT INTO tasks (kind, lane, agent_id, session_id, payload, status, attempts, not_before, created_at)"
            " VALUES ('deliver', 'deliver:s-push', 'a1', 's-push',"
            ' \'{"channel": "webpush", "target": null, "title": "t", "body": "b", "session_id": "s-push"}\','
            " 'pending', 0, ?, ?)",
            (ts, ts),
        )

    migrate(path, agents_dir)

    with sqlite3.connect(path) as conn:
        assert dict(conn.execute("SELECT id, notify FROM sessions")) == {"s-push": 1, "s-none": 0}
        payload = conn.execute("SELECT payload FROM tasks").fetchone()[0]
        assert json.loads(payload) == {"title": "t", "body": "b", "session_id": "s-push"}
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert "webhooks" not in tables


def test_drop_triggers_migration_refuses_existing_telegram_sessions(tmp_path):
    path, agents_dir = tmp_path / "m.db", tmp_path / "agents"
    migrate(path, agents_dir, target=BEFORE_DROP_TRIGGERS)
    ts = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO agents VALUES ('a1', 'n', 'd', 'm:x', '', 5, 0, 0, ?, ?)", (ts, ts))
        conn.execute(
            "INSERT INTO sessions VALUES ('s', 'a1', 't', 'telegram', '42', 'telegram', '42', 0, ?, ?)",
            (ts, ts),
        )

    with pytest.raises(RuntimeError, match="removed triggers still have data"):
        migrate(path, agents_dir)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == BEFORE_DROP_TRIGGERS
