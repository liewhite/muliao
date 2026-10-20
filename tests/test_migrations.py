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

    monkeypatch.setattr(muliao.db.command, "upgrade", broken_upgrade)
    with pytest.raises(RuntimeError, match="migration bug"):
        migrate(path)

    assert _tables(path) == before


def test_foreign_key_violation_rolls_back_migration(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    migrate(path)

    def orphaning_upgrade(cfg, _revision):  # noqa: ANN001
        cfg.attributes["connection"].exec_driver_sql(
            "INSERT INTO sessions (id, agent_id, title, origin, private, unread, created_at, updated_at) "
            "VALUES ('s', 'ghost', 't', 'user', 0, 0, '2026-01-01', '2026-01-01')"
        )

    monkeypatch.setattr(muliao.db.command, "upgrade", orphaning_upgrade)
    with pytest.raises(RuntimeError, match="foreign key violations"):
        migrate(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from sessions").fetchone()[0] == 0


def test_existing_deliver_tasks_become_notifications_and_only_reference_them(tmp_path):
    path = tmp_path / "m.db"
    migrate(path, "0e63aa5dab99")
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO tasks (id, kind, lane, payload, status, attempts, not_before, created_at) "
            "VALUES (?, 'deliver', 'deliver:system', ?, ?, 0, '2026-01-01', '2026-01-02 03:04:05')",
            [
                (7, '{"title": "前置检查出错", "body": "timed out", "session_id": null}', "done"),
                (9, '{"title": "t", "body": "b", "session_id": null}', "pending"),
            ],
        )
    migrate(path)

    with sqlite3.connect(path) as conn:
        notes = conn.execute(
            "select id, title, body, session_id, unread, created_at from notifications"
        ).fetchall()
        payloads = conn.execute("select payload from tasks order by id").fetchall()
        conn.execute(
            "INSERT INTO notifications (title, body, unread, created_at) VALUES ('n', 'n', 1, '2026-01-03')"
        )
        next_id = conn.execute("select max(id) from notifications").fetchone()[0]
    # 投递过的视为已读，还没投递的保持未读
    assert notes == [
        (7, "前置检查出错", "timed out", None, 0, "2026-01-02 03:04:05"),
        (9, "t", "b", None, 1, "2026-01-02 03:04:05"),
    ]
    assert payloads == [('{"notification_id":7}',), ('{"notification_id":9}',)]
    # 沿用 task id 后，新通知的 id 仍在它们之后（「已读到哪一条」的游标单调）
    assert next_id == 10


def test_existing_turn_payloads_get_an_empty_report_to(tmp_path):
    path = tmp_path / "m.db"
    migrate(path, "21fe4c2a53ff")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO tasks (kind, lane, payload, status, attempts, not_before, created_at) VALUES "
            '(\'turn\', \'l\', \'{"prompt": "hi", "reply_to": "user", "attachments": [], "answers": {}}\', '
            "'done', 1, '2026-01-01', '2026-01-01')"
        )
    migrate(path, "a3339b22b82a")
    with sqlite3.connect(path) as conn:
        [(payload,)] = conn.execute("select payload from tasks").fetchall()
    assert '"report_to":null' in payload.replace(" ", "")


def test_unfinished_turns_and_queued_events_become_mail(tmp_path):
    path = tmp_path / "m.db"
    migrate(path, "a3339b22b82a")
    turn = (
        "INSERT INTO tasks (id, kind, lane, session_id, agent_id, payload, status, attempts, not_before, created_at) "
        "VALUES (?, 'turn', 'session:s', 's', 'a', ?, ?, 1, '2026-01-01', ?)"
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO agents (id, name, description, model, tool_groups, reflect_every, turns_since_reflection, "
            "reflection_checkpoint, created_at, updated_at) VALUES ('a', 'a', '', 'm', '[]', 5, 0, 0, "
            "'2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO sessions (id, agent_id, title, origin, private, unread, created_at, updated_at) "
            "VALUES ('s', 'a', 't', 'user', 0, 0, '2026-01-01', '2026-01-01')"
        )
        # 失败之后会话又完成过别的 turn：旧系统里它只是一条失败记录，不转成信件
        conn.execute(
            turn,
            (
                1,
                '{"prompt": "superseded", "reply_to": "user", "attachments": [], "answers": {}, "report_to": null}',
                "failed",
                "2026-01-01 00:00:00",
            ),
        )
        conn.execute(
            turn,
            (
                2,
                '{"prompt": "old", "reply_to": "user", "attachments": [], "answers": {}, "report_to": null}',
                "done",
                "2026-01-01 00:00:01",
            ),
        )
        conn.execute(
            turn,
            (
                3,
                '{"prompt": "go on", "reply_to": "user", "attachments": ["f1"], '
                '"answers": {"q1": {"answered": true, "answers": [], "note": null}}, '
                '"report_to": {"session_id": "p", "reply_to": "user"}}',
                "pending",
                "2026-01-01 00:00:02",
            ),
        )
        conn.execute(
            turn,
            (
                4,
                '{"prompt": "failed one", "reply_to": "none", "attachments": [], '
                '"answers": {}, "report_to": {"session_id": "p", "reply_to": "user"}}',
                "failed",
                "2026-01-01 00:00:03",
            ),
        )
        conn.execute(
            "INSERT INTO session_events (session_id, source, content, reply_to, created_at) "
            "VALUES ('s', 'job:1', 'done', 'user', '2026-01-01 00:00:04')"
        )
        conn.execute(
            "INSERT INTO jobs (session_id, agent_id, command, secrets, status, log_path, reply_to, started_at) "
            "VALUES ('s', 'a', 'true', '[]', 'exited', 'jobs/1.log', 'user', '2026-01-01')"
        )
    migrate(path)

    with sqlite3.connect(path) as conn:
        mail = conn.execute(
            "select kind, source, content, attachments, answers, awaited, report_to, state, created_at "
            "from mail order by id"
        ).fetchall()
        tasks = conn.execute("select id, status, payload from tasks order by id").fetchall()
        awaited = conn.execute("select awaited from jobs").fetchall()
    assert mail == [
        (
            "answer",
            None,
            "",
            "[]",
            '{"q1":{"answered":true,"answers":[],"note":null}}',
            1,
            None,
            "queued",
            "2026-01-01 00:00:02",
        ),
        (
            "message",
            None,
            "go on",
            '["f1"]',
            "{}",
            1,
            '{"session_id":"p","awaited":true}',
            "queued",
            "2026-01-01 00:00:02",
        ),
        ("message", None, "failed one", "[]", "{}", 0, None, "queued", "2026-01-01 00:00:03"),
        ("event", "job:1", "done", "[]", "{}", 1, None, "queued", "2026-01-01 00:00:04"),
    ]
    # 信件按到达顺序编号；失败 turn 的委派当时已经交回过失败，信件不再带交回地址
    # 排队的 turn 删除，失败的保留等用户重试；payload 都清空
    assert tasks == [(1, "failed", "{}"), (2, "done", "{}"), (4, "failed", "{}")]
    assert awaited == [(1,)]
