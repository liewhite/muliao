import json
import sqlite3

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

import muliao.platform.db
from muliao.platform.db import Base, Database, alembic_config, create_engine, include_name, migrate

# 统一语言（#246）改列名之前的最后一版：测更早迁移的用例停在这里，再从这里降级
BEFORE_UNIFIED_LANGUAGE = "101643b15832"


def _tables(path) -> set[str]:  # noqa: ANN001
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


async def test_migrated_schema_matches_models(tmp_path):
    """改了模型却没写迁移时失败：运行 `uv run alembic revision --autogenerate` 补上。"""
    migrate(tmp_path / "m.db")
    engine = create_engine(tmp_path / "m.db")

    def diff(conn):  # noqa: ANN001, ANN202
        ctx = MigrationContext.configure(conn, opts={"render_as_batch": True, "include_name": include_name})
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


def test_rebuilding_the_triggers_table_does_not_reuse_deleted_ids(tmp_path):
    """finished_at 迁移重建了 triggers 表：被删掉的最大 id 升级、降级之后都不复用（会话的 origin_ref 指向它）。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, "cd85203e73a8")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, tool_groups, reflect_every, created_at, "
            "updated_at) values ('a1a1a1a1a1a1', 'alpha', '', 'm', '[]', 0, '2026-01-01', '2026-01-01')"
        )
        for name in ("a", "b", "c"):
            _insert_trigger(conn, name)
        conn.execute("delete from triggers where name in ('b', 'c')")

    def next_id(conn: sqlite3.Connection, name: str) -> int:
        _insert_trigger(conn, name)
        return conn.execute("select id from triggers where name = ?", (name,)).fetchone()[0]

    migrate(path)
    with sqlite3.connect(path) as conn:
        assert next_id(conn, "d") == 4
        conn.execute("delete from triggers")

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "cd85203e73a8")
    with sqlite3.connect(path) as conn:
        # 降级重建时表是空的：新表没有序号行，也要补回去
        assert next_id(conn, "e") == 5


def test_issue_assignees_merge_into_one_column_and_back(tmp_path):
    """被指派人两列合成一列（agent id / "user" / NULL），降级换算回来；认领、让出的时间线删除；编号仍不复用。"""
    from alembic import command

    path = tmp_path / "m.db"
    migrate(path, "663aa18d63fb")
    with sqlite3.connect(path) as conn:
        for id_, agent, to_user in [(1, "a1a1a1a1a1a1", 0), (2, None, 1), (3, None, 0)]:
            conn.execute(
                "insert into issues (id, title, description, status, priority, assignee_agent_id, "
                "assigned_to_user, sessions, created_at, updated_at) "
                "values (?, 't', '', 'todo', 'none', ?, ?, '{}', '2026-01-01', '2026-01-01')",
                (id_, agent, to_user),
            )
        for kind in ("comment", "claimed", "released"):
            conn.execute(
                "insert into issue_activities (issue_id, created_at, kind) values (1, '2026-01-01', ?)",
                (kind,),
            )
        conn.execute("delete from issues where id = 3")

    migrate(path, BEFORE_UNIFIED_LANGUAGE)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id, assignee from issues order by id").fetchall() == [
            (1, "a1a1a1a1a1a1"),
            (2, "user"),
        ]
        assert conn.execute("select kind from issue_activities").fetchall() == [("comment",)]
        conn.execute(
            "insert into issues (title, description, status, priority, created_at, updated_at) "
            "values ('t', '', 'todo', 'none', '2026-01-01', '2026-01-01')"
        )
        assert conn.execute("select max(id) from issues").fetchone()[0] == 4

    from muliao.platform.db import _migration_engine

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "663aa18d63fb")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "select id, assignee_agent_id, assigned_to_user from issues order by id"
        ).fetchall() == [(1, "a1a1a1a1a1a1", 0), (2, None, 1), (4, None, 0)]


def test_issue_dependencies_downgrade_drops_the_table_and_its_activities(tmp_path):
    """降级删掉依赖表与两种依赖动态（旧版本读到未知的 kind 会报错），其余时间线保留。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, BEFORE_UNIFIED_LANGUAGE)
    with sqlite3.connect(path) as conn:
        for id_ in (1, 2):
            conn.execute(
                "insert into issues (id, title, description, status, priority, created_at, updated_at) "
                "values (?, 't', '', 'todo', 'none', '2026-01-01', '2026-01-01')",
                (id_,),
            )
        conn.execute("insert into issue_dependencies (issue_id, depends_on_id) values (2, 1)")
        for kind in ("comment", "depends_on", "dependency"):
            conn.execute(
                "insert into issue_activities (issue_id, created_at, kind) values (2, '2026-01-01', ?)",
                (kind,),
            )
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "6bba488992eb")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select kind from issue_activities").fetchall() == [("comment",)]
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type = 'table'")}
        assert "issue_dependencies" not in tables


def test_dropping_notifications_issue_id_keeps_notifications_and_their_ids(tmp_path):
    """降级删 notifications.issue_id 要重建表：通知保留，删掉的最大 id 不复用（id 是已读游标）。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with sqlite3.connect(path) as conn:
        for id_, issue_id in ((1, 7), (2, None)):
            conn.execute(
                "insert into notifications (id, title, body, issue_id, unread, created_at) "
                "values (?, 't', 'b', ?, 1, '2026-01-01')",
                (id_, issue_id),
            )
        conn.execute("delete from notifications where id = 2")
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "b9716fea0122")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id from notifications").fetchall() == [(1,)]
        conn.execute(
            "insert into notifications (title, body, unread, created_at) values ('n', 'b', 1, '2026-01-01')"
        )
        assert conn.execute("select max(id) from notifications").fetchone() == (3,)


def test_dropping_reflections_keeps_agents_other_tasks_and_usage(tmp_path):
    """去掉强制反思（#116）：反思的表、agents.reflect_every、reflection 任务与投给反思订阅者的事件删掉；agent、别的任务与 kind=reflection
    的用量账目保留。降级恢复结构，reflect_every 回填 30。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "b62e70969c23")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, tool_groups, reflect_every, created_at, updated_at) "
            "values ('a1', 'alpha', 'd', 'p:m', '[]', 10, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "insert into reflections (agent_id, from_message_id, to_message_id, output, messages, created_at) "
            "values ('a1', 0, 5, 'o', '[]', '2026-01-01')"
        )
        conn.execute("insert into reflection_checkpoints (agent_id, message_id) values ('a1', 5)")
        for id_, kind, lane, status in (
            (1, "reflection", "reflection:a1", "pending"),
            (2, "reflection", "reflection:a1", "done"),
            (3, "run", "session:s1", "done"),
            (4, "event", "reflection-events:a1", "failed"),
            (5, "event", "notifications:s1", "pending"),
        ):
            conn.execute(
                "insert into tasks (id, kind, lane, agent_id, payload, status, attempts, not_before, created_at) "
                "values (?, ?, ?, 'a1', '{}', ?, 0, '2026-01-01', '2026-01-01')",
                (id_, kind, lane, status),
            )
        conn.execute(
            "insert into model_usage (created_at, agent_id, kind, model, requests, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens) values ('2026-01-01', 'a1', 'reflection', 'p:m', 1, 2, 3, 0, 0)"
        )
    migrate(path)
    with sqlite3.connect(path) as conn:
        assert {"reflections", "reflection_checkpoints"}.isdisjoint(_tables(path))
        assert "reflect_every" not in {r[1] for r in conn.execute("pragma table_info(agents)")}
        assert conn.execute("select id, name from agents").fetchall() == [("a1", "alpha")]
        assert conn.execute("select id, kind from tasks").fetchall() == [(3, "run"), (5, "event")]
        assert conn.execute("select kind from model_usage").fetchall() == [("reflection",)]
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "b62e70969c23")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select reflect_every from agents").fetchall() == [(30,)]
        assert conn.execute("select count(*) from reflections").fetchone() == (0,)


def test_check_ins_are_backfilled_on_mail_and_message_metadata(tmp_path):
    """运行中检查显式标记（#121）：按当时检查信件的文字回填 mail.check_in 与请求 metadata 的 muliao_check_ins；
    结局事件不算检查。降级删列并去掉 metadata 里的标记。"""
    import json

    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "b97a9752ebad")
    job_check = "Background job 3 is still running (0m05s so far).\nCommand: x\nNo output yet."
    job_end = "Background job 3 finished with exit code 0 after 1m00s."
    thread_check = (
        "Thread 'fix' (ops) is waiting for work it started. Keep waiting, you will get an event with its answer; "
        "if it is no longer needed, call stop_thread('fix')."
    )
    thread_end = "Thread 'fix' (ops) finished your task. Its answer:\nok"

    def event(source: str, content: str) -> str:
        return f'<event source="{source}">\n{content}\n</event>'

    request = {
        "parts": [
            {"content": "now", "part_kind": "user-prompt"},
            {
                "content": [
                    event("job:3", job_check),
                    "hi",
                    event("thread:t1", thread_end),
                    event("thread:t1", thread_check),
                ],
                "part_kind": "user-prompt",
            },
        ],
        "kind": "request",
        "metadata": {"muliao_run_context": 0, "muliao_events": [0, 2, 3]},
    }
    plain = {"parts": [{"content": "hello", "part_kind": "user-prompt"}], "kind": "request", "metadata": None}
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into sessions (id, agent_id, title, origin, kind, unread, created_at, updated_at) "
            "values ('s1', 'a1', 't', 'user', 'chat', 0, '2026-01-01', '2026-01-01')"
        )
        for id_, kind, source, content in (
            (1, "event", "job:3", job_check),
            (2, "event", "job:3", job_end),
            (3, "event", "thread:t1", thread_check),
            (4, "event", "thread:t1", thread_end),
            (5, "message", None, job_check),
        ):
            conn.execute(
                "insert into mail (id, session_id, kind, source, content, attachments, answers, awaited, "
                "correlation_settled, state, created_at) values (?, 's1', ?, ?, ?, '[]', '{}', 0, 0, 'consumed', "
                "'2026-01-01')",
                (id_, kind, source, content),
            )
        for id_, payload in ((1, request), (2, plain)):
            conn.execute(
                "insert into messages (id, session_id, task_id, payload, created_at) values (?, 's1', 1, ?, '2026-01-01')",
                (id_, json.dumps(payload)),
            )
    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id from mail where check_in order by id").fetchall() == [(1,), (3,)]
        metadata = json.loads(conn.execute("select payload from messages where id = 1").fetchone()[0])[
            "metadata"
        ]
        assert metadata == {"muliao_run_context": 0, "muliao_events": [0, 2, 3], "muliao_check_ins": [0, 3]}
        assert json.loads(conn.execute("select payload from messages where id = 2").fetchone()[0]) == plain
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "b97a9752ebad")
    with sqlite3.connect(path) as conn:
        assert "check_in" not in {r[1] for r in conn.execute("pragma table_info(mail)")}
        metadata = json.loads(conn.execute("select payload from messages where id = 1").fetchone()[0])[
            "metadata"
        ]
        assert metadata == {"muliao_run_context": 0, "muliao_events": [0, 2, 3]}


def test_agents_is_default_adds_no_default_and_downgrades(tmp_path):
    """agents.is_default（#132）：已有 agent 升级后都不是默认；部分唯一索引只允许一个默认；降级删掉列，agent 保留。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "75eeddddf94a")
    with sqlite3.connect(path) as conn:
        for id_ in ("a1", "a2"):
            conn.execute(
                "insert into agents (id, name, description, model, tool_groups, created_at, updated_at) "
                "values (?, ?, 'd', 'p:m', '[]', '2026-01-01', '2026-01-01')",
                (id_, id_),
            )
    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id, is_default from agents order by id").fetchall() == [
            ("a1", 0),
            ("a2", 0),
        ]
        conn.execute("update agents set is_default = 1 where id = 'a1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("update agents set is_default = 1 where id = 'a2'")
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "75eeddddf94a")
    with sqlite3.connect(path) as conn:
        assert "is_default" not in {r[1] for r in conn.execute("pragma table_info(agents)")}
        assert conn.execute("select id from agents order by id").fetchall() == [("a1",), ("a2",)]


def test_agent_history_is_append_only_searchable_and_downgrades(tmp_path):
    """agent_history（#155）：插入即进 FTS 虚表；UPDATE / DELETE 被触发器拒绝；降级删掉表、虚表与触发器。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agent_history (agent_id, day, title, summary, refs, importance, created_at) "
            "values ('a1', '2026-10-22', '部署历史表', '加了检索', '[\"#155\"]', 2, '2026-10-23')"
        )
        assert conn.execute(
            "select rowid from agent_history_fts where agent_history_fts match '\"历史表\"'"
        ).fetchall() == [(1,)]
        for sql in ("update agent_history set title = 'x'", "delete from agent_history"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "c0012ec9d2f2")
    with sqlite3.connect(path) as conn:
        names = {r[0] for r in conn.execute("select name from sqlite_master")}
    assert not {n for n in names if n.startswith("agent_history")}
    migrate(path)
    assert {"agent_history", "agent_history_fts"} <= _tables(path)


def test_agents_thinking_defaults_to_default_and_downgrades(tmp_path):
    """agents.thinking（#171）：已有 agent 升级后为 "default"（不传 thinking，行为不变）；降级删掉列，agent 保留。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "f88bba24ade7")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, tool_groups, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', '[]', '2026-01-01', '2026-01-01')"
        )
    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id, thinking from agents").fetchall() == [("a1", "default")]
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "f88bba24ade7")
    with sqlite3.connect(path) as conn:
        assert "thinking" not in {r[1] for r in conn.execute("pragma table_info(agents)")}
        assert conn.execute("select id from agents").fetchall() == [("a1",)]


def test_dropping_sessions_unread_keeps_sessions_their_messages_and_backfills_on_downgrade(tmp_path):
    """会话未读改为从通知派生（#175）：升级删 sessions.unread（重建 sessions 表，会话、它的消息与通知都保留）；降级
    加回这一列，按未读通知回填。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "bd1e7f7a8324")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, tool_groups, is_default, thinking, created_at, "
            "updated_at) values ('a1a1a1a1a1a1', 'a', '', 'm', '[]', 0, 'default', '2026-01-01', '2026-01-01')"
        )
        for id_, unread in (("s1", 1), ("s2", 0)):
            conn.execute(
                "insert into sessions (id, agent_id, title, origin, kind, unread, created_at, updated_at) "
                "values (?, 'a1a1a1a1a1a1', 't', 'user', 'conversation', ?, '2026-01-01', '2026-01-01')",
                (id_, unread),
            )
        conn.execute(
            "insert into messages (session_id, task_id, payload, created_at) "
            """values ('s1', 1, '{"parts": [], "kind": "request"}', '2026-01-01')"""
        )
        conn.execute(
            "insert into notifications (title, body, session_id, unread, created_at) "
            "values ('n', 'b', 's2', 1, '2026-01-01')"
        )
    migrate(path)
    with sqlite3.connect(path) as conn:
        columns = [r[1] for r in conn.execute("pragma table_info(sessions)")]
        assert "unread" not in columns
        assert conn.execute("select id from sessions order by id").fetchall() == [("s1",), ("s2",)]
        assert conn.execute("select session_id from messages").fetchall() == [("s1",)]
        assert conn.execute("select session_id from notifications").fetchall() == [("s2",)]
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "bd1e7f7a8324")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id, unread from sessions order by id").fetchall() == [
            ("s1", 0),
            ("s2", 1),
        ]


def test_tool_groups_become_same_named_roles_and_downgrade_expands_admin(tmp_path):
    """工具权限改为角色（#172）：tool_groups 原样变成 roles（每个组都有同名内置角色），allow / deny 为空；降级把 admin
    展开成全部组、去掉自建角色，删掉 roles 表。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "820a32ee48fd")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, tool_groups, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', '[\"notify\", \"issues\"]', '2026-01-01', '2026-01-01')"
        )
    with _migration_engine(path).begin() as connection:
        command.upgrade(alembic_config(connection), "cba7dcdfe011")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select roles, allow, deny from agents").fetchall() == [
            ('["notify", "issues"]', "[]", "[]")
        ]
        conn.execute(
            "insert into agents (id, name, description, model, roles, created_at, updated_at) "
            "values ('a2', 'a2', 'd', 'p:m', '[\"admin\", \"mine\"]', '2026-01-01', '2026-01-01')"
        )
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "820a32ee48fd")
    with sqlite3.connect(path) as conn:
        rows = dict(conn.execute("select id, tool_groups from agents").fetchall())
        assert {r[1] for r in conn.execute("pragma table_info(agents)")} & {"roles", "allow", "deny"} == set()
    assert rows["a1"] == '["notify", "issues"]'
    assert rows["a2"].startswith('["workspace", ') and rows["a2"].endswith('"web"]')
    assert "roles" not in _tables(path)


def test_roles_become_admin_or_agent_and_downgrade_expands_agent(tmp_path):
    """两个内置角色（#180）：roles 里有 admin 的为 ["admin"]，其余为 ["agent"]，allow / deny 清空，mcp_servers 为 []。
    降级时 agent 展开成旧工具组（勾选过公共 MCP 的加 mcp），admin 不变。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "cba7dcdfe011")
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into agents (id, name, description, model, roles, deny, created_at, updated_at) "
            "values (?, ?, 'd', 'p:m', ?, ?, '2026-01-01', '2026-01-01')",
            [
                ("a1", "a1", '["admin"]', "[]"),
                ("a2", "a2", '["workspace", "agents", "mcp"]', '["agent:create"]'),
            ],
        )
    migrate(path, "e8964a50067c")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "select id, roles, allow, deny, mcp_servers from agents order by id"
        ).fetchall() == [
            ("a1", '["admin"]', "[]", "[]", "[]"),
            ("a2", '["agent"]', "[]", "[]", "[]"),
        ]
        conn.execute(
            "update agents set mcp_servers = '[\"playwright\"]', allow = '[\"skill.public:write\"]' where id = 'a2'"
        )
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "cba7dcdfe011")
    with sqlite3.connect(path) as conn:
        rows = dict(conn.execute("select id, roles from agents").fetchall())
        assert "mcp_servers" not in {r[1] for r in conn.execute("pragma table_info(agents)")}
    assert rows["a1"] == '["admin"]'
    groups = json.loads(rows["a2"])
    assert (
        "mcp" in groups and "workspace" in groups and not {"agents", "public_skills", "agent"} & set(groups)
    )
    # 降级补上 agent:get，原有的 allow 保留
    with sqlite3.connect(path) as conn:
        allow = dict(conn.execute("select id, allow from agents").fetchall())
    assert allow == {"a1": "[]", "a2": '["skill.public:write", "agent:get"]'}


def test_threads_per_agent_heartbeats_and_timeouts_backfill_and_downgrade(tmp_path):
    """#217：thread 唯一索引加上 agent；jobs.check_seconds 回填 60；report_back 委派的占用 check_seconds 回填 60，ask_agent
    的不设 deadline。降级时同一父会话里重名的 thread 改名（保留最早的），旧索引能建起来，新列删掉。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "e8964a50067c")
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into agents (id, name, description, model, roles, created_at, updated_at) "
            "values (?, ?, 'd', 'p:m', '[\"agent\"]', '2026-01-01', '2026-01-01')",
            [("a1", "a1"), ("a2", "a2")],
        )
        conn.execute(
            "insert into sessions (id, agent_id, title, origin, kind, created_at, updated_at) "
            "values ('p', 'a1', 't', 'user', 'conversation', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "insert into jobs (session_id, agent_id, command, secrets, status, log_path, started_at) "
            "values ('p', 'a1', 'true', '[]', 'exited', 'jobs/1.log', '2026-01-01')"
        )
        conn.executemany(
            "insert into session_holds (session_id, holder, call_id, awaited, status, created_at) "
            "values ('p', ?, ?, 0, 'active', '2026-01-01')",
            [("delegation:p:k1", None), ("delegation:p:k2", "call"), ("job:1", None)],
        )
    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select check_seconds from jobs").fetchall() == [(60.0,)]
        # 委派的参数后来搬进了 delegations（#244）；两项委派都没有 thread 信件
        assert conn.execute(
            "select holder, thread_session_id, check_seconds, deadline from delegations order by holder"
        ).fetchall() == [
            ("delegation:p:k1", None, 60.0, None),
            ("delegation:p:k2", None, None, None),
        ]
        # 同一父会话、同名、不同 agent：现在是两个 thread
        conn.executemany(
            "insert into sessions (id, agent_id, title, origin, origin_ref, parent_session_id, thread, kind, "
            "created_at, updated_at) values (?, ?, 't', 'delegation', 'p', 'p', 'review', 'thread', ?, ?)",
            [("c1", "a1", "2026-01-02", "2026-01-02"), ("c2", "a2", "2026-01-03", "2026-01-03")],
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "insert into sessions (id, agent_id, title, origin, origin_ref, parent_session_id, thread, kind, "
                "created_at, updated_at) values "
                "('c3', 'a1', 't', 'delegation', 'p', 'p', 'review', 'thread', '2026-01-04', '2026-01-04')"
            )
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "e8964a50067c")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "select id, thread from sessions where thread is not null order by id"
        ).fetchall() == [
            ("c1", "review"),
            ("c2", "review#a2"),
        ]
        assert "check_seconds" not in {r[1] for r in conn.execute("pragma table_info(jobs)")}
        assert not {"check_seconds", "deadline"} & {
            r[1] for r in conn.execute("pragma table_info(session_holds)")
        }


def test_agents_group_and_archived_at_start_empty_and_downgrade(tmp_path):
    """agents.group / archived_at（#219）：已有 agent 升级后不分组、未归档；降级删掉两列，agent 保留。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "aa38462ddc18")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, roles, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', '[\"agent\"]', '2026-01-01', '2026-01-01')"
        )
    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('select id, "group", archived_at from agents').fetchall() == [("a1", None, None)]
        conn.execute("update agents set \"group\" = 'g', archived_at = '2026-10-26' where id = 'a1'")
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "aa38462ddc18")
    with sqlite3.connect(path) as conn:
        assert not {"group", "archived_at"} & {r[1] for r in conn.execute("pragma table_info(agents)")}
        assert conn.execute("select id from agents").fetchall() == [("a1",)]


def test_resources_and_secrets_move_into_items_with_unique_names_and_downgrade(tmp_path):
    """#230：资源仓库（数据库同目录的 resources/）里的 skill、MCP、记忆与 secrets 表导入 items / memories，每项记一版；
    重名的私有项改名，被它遮蔽的公共 MCP 勾选去掉；同名 secret 合并成公共的；旧仓库改名留着。降级重建 secrets、改回目录。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "75cc40ae1d3d")
    a1, a2 = "a1a1a1a1a1a1", "b2b2b2b2b2b2"
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into agents (id, name, description, model, roles, mcp_servers, created_at, updated_at) "
            "values (?, ?, 'd', 'p:m', '[\"agent\"]', '[\"pw\"]', '2026-01-01', '2026-01-01')",
            [(a1, "alpha"), (a2, "Beta Bot")],
        )
        conn.executemany(
            "insert into secrets (owner, key, ciphertext, description, updated_at) values (?, ?, ?, '', ?)",
            [
                (a1, "SHARED", b"c1", "2026-01-01 00:00:00"),
                (a2, "SHARED", b"c2", "2026-01-02 00:00:00"),
                (a1, "OWN", b"c3", "2026-01-01 00:00:00"),
            ],
        )
    root = tmp_path / "resources"

    def write(rel: str, text: str, mode: int = 0o644) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        p.chmod(mode)

    def skill(name: str, body: str) -> str:
        return f"---\nname: {name}\ndescription: {name} skill\n---\n{body}\n"

    write("public/MEMORY.md", "global rule")
    write(f"agents/{a1}/MEMORY.md", "see ~/.muliao/data/resources/public/skills/shared/run.sh")
    write("public/skills/shared/SKILL.md", skill("shared", "run resources/public/skills/shared/run.sh"))
    write("public/skills/shared/run.sh", "echo hi\n", 0o755)
    write("public/skills/shared/.venv/x.py", "ignored")
    write(
        f"agents/{a1}/skills/shared/SKILL.md",
        skill("shared", f"run resources/agents/{a1}/skills/shared/x.sh"),
    )
    # 读不出来的公共 skill 不导入，也不让同名的私有 skill 改名
    write("public/skills/broken/SKILL.md", "---\nname: [oops\n---\n")
    write(f"agents/{a2}/skills/broken/SKILL.md", skill("broken", "fine"))
    write(f"agents/{a2}/skills/solo/SKILL.md", skill("solo", f"see resources/agents/{a2}/skills/solo/"))
    config = {"transport": "stdio", "command": "x", "args": [], "url": None, "env": {}, "headers": {}}
    write("public/mcp/pw.json", json.dumps({**config, "enabled": True}))
    write(f"agents/{a1}/mcp/pw.json", json.dumps({**config, "enabled": True}))
    write(f"agents/{a2}/mcp/pw.json", json.dumps({**config, "enabled": False}))

    migrate(path, "f369adb6b551")

    assert not root.exists() and (tmp_path / "resources.imported").is_dir()
    with sqlite3.connect(path) as conn:
        items = {
            (k, n): (o, e) for k, n, o, e in conn.execute("select kind, name, owner, enabled from items")
        }
        assert items == {
            ("skill", "shared"): (None, 1),
            ("skill", "alpha-shared"): (a1, 1),
            ("skill", "solo"): (a2, 1),
            ("skill", "broken"): (a2, 1),
            ("mcp", "pw"): (None, 1),
            ("mcp", "alpha-pw"): (a1, 1),
            ("mcp", f"{a2}-pw"): (a2, 0),
            ("secret", "SHARED"): (None, 1),
            ("secret", "OWN"): (a1, 1),
        }
        renamed = conn.execute("select content from items where name = 'alpha-shared'").fetchone()[0]
        assert "name: alpha-shared" in renamed and "run skills/alpha-shared/x.sh" in renamed
        public = conn.execute("select content from items where name = 'shared'").fetchone()[0]
        assert "run skills/shared/run.sh" in public
        assert (
            conn.execute("select content from items where name = 'solo'")
            .fetchone()[0]
            .endswith("see skills/solo/\n")
        )
        assert conn.execute("select path, executable from item_files").fetchall() == [("run.sh", 1)]
        assert conn.execute("select secret from items where name = 'SHARED'").fetchone()[0] == b"c2"
        # alpha 的私有 pw 原本遮蔽公共的：去掉勾选；Beta 的私有 pw 停用着，不遮蔽，勾选保留
        assert dict(conn.execute("select id, mcp_servers from agents")) == {a1: "[]", a2: '["pw"]'}
        memories = dict(conn.execute("select owner, content from memories"))
        assert memories == {None: "global rule", a1: "see ~/.muliao/data/skills/shared/run.sh", a2: ""}
        revisions = conn.execute("select subject, name, before, author from revisions").fetchall()
        set_at = json.loads(conn.execute("select after from revisions where name = 'OWN'").fetchone()[0])
        assert set_at["value_set_at"] == "2026-01-01T00:00:00+00:00"
        assert len(revisions) == 12 and all(r[2] is None and r[3] == "muliao" for r in revisions)

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "75cc40ae1d3d")
    assert root.is_dir() and not (tmp_path / "resources.imported").exists()
    with sqlite3.connect(path) as conn:
        assert set(conn.execute("select owner, key, ciphertext from secrets")) == {
            (None, "SHARED", b"c2"),
            (a1, "OWN", b"c3"),
        }
        assert "items" not in {
            r[0] for r in conn.execute("select name from sqlite_master where type='table'")
        }


def test_rules_become_kind_name_base_collects_platform_items_and_mcp_selections_become_roles(tmp_path):
    """#235：内置 agent 角色去掉（base 隐含）；权限 id 改写成 `tool:<名>`（删掉的工具去掉、匹配全部的成 `tool:*`）；base
    收录归平台的 skill 与 secret；mcp_servers 勾选成同名角色，删掉列。降级还原。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, "f369adb6b551")
    t = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into agents (id, name, description, model, roles, allow, deny, mcp_servers, created_at, "
            "updated_at) values (?, ?, 'd', 'p:m', ?, ?, ?, ?, ?, ?)",
            [
                ("a1", "a1", '["admin"]', '["*"]', "[]", "[]", t, t),
                ("a2", "a2", '["agent", "rev"]', '["issue:*"]', '["shell:*"]', '["pw"]', t, t),
                ("a3", "a3", '["agent"]', "[]", "[]", '["gone"]', t, t),
            ],
        )
        conn.execute(
            "insert into roles (name, description, allow, deny, created_at, updated_at) values "
            "('rev', 'd', '[\"skill.public:*\", \"issue:list\"]', '[]', ?, ?)",
            (t, t),
        )
        conn.executemany(
            "insert into items (kind, name, owner, description, content, enabled, created_at, updated_at) "
            "values (?, ?, ?, '', '', 1, ?, ?)",
            [
                ("skill", "s1", None, t, t),
                ("skill", "own", "a1", t, t),
                ("secret", "K", None, t, t),
                ("mcp", "pw", None, t, t),
            ],
        )

    migrate(path, "acd7fd2ea877")

    with sqlite3.connect(path) as conn:
        assert "mcp_servers" not in {r[1] for r in conn.execute("pragma table_info(agents)")}
        agents = {r[0]: r[1:] for r in conn.execute("select id, roles, allow, deny from agents")}
        roles = {
            r[0]: (json.loads(r[1]), json.loads(r[2]))
            for r in conn.execute("select name, allow, deny from roles")
        }
    issue_tools = [f"tool:issue_{n}" for n in ("list", "show", "create", "update", "comment")]
    assert agents == {
        "a1": ('["admin"]', '["tool:*"]', "[]"),
        "a2": ('["rev", "pw"]', json.dumps(issue_tools), '["tool:run_shell"]'),
        "a3": ("[]", "[]", "[]"),
    }
    assert roles == {
        "rev": (["tool:issue_list"], []),
        "base": (["secret:K", "skill:s1"], []),
        "pw": (["mcp:pw"], []),
    }

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "f369adb6b551")
    with sqlite3.connect(path) as conn:
        agents = {r[0]: r[1:] for r in conn.execute("select id, roles, allow, deny, mcp_servers from agents")}
        roles = {r[0]: r[1] for r in conn.execute("select name, allow from roles")}
    assert agents == {
        "a1": ('["admin"]', '["*"]', "[]", "[]"),
        "a2": (
            '["agent", "rev"]',
            '["issue:list", "issue:show", "issue:create", "issue:update", "issue:comment"]',
            '["shell:run"]',
            '["pw"]',
        ),
        "a3": ('["agent"]', "[]", "[]", "[]"),
    }
    assert roles == {"rev": '["issue:list"]'}


def test_converting_rules_refuses_data_it_cannot_keep_unchanged(tmp_path):
    """#235 换算的前提：每个 agent 带 agent 或 admin 角色（否则隐含 base 会多出工具）；不满足就报错回滚。"""
    path = tmp_path / "m.db"
    migrate(path, "f369adb6b551")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, roles, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', '[\"rev\"]', '2026-01-01', '2026-01-01')"
        )
    with pytest.raises(RuntimeError, match="neither the agent nor the admin role"):
        migrate(path)
    with sqlite3.connect(path) as conn:
        assert "mcp_servers" in {r[1] for r in conn.execute("pragma table_info(agents)")}


def test_agent_rules_move_to_their_own_table_and_downgrade(tmp_path):
    """#242：agents 的 roles / allow / deny 搬进 agent_rules 后删列（原生 DROP COLUMN，agents 不重建），条目与记忆不变。
    降级搬回三列、删表。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, "acd7fd2ea877")
    t = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into agents (id, name, description, model, roles, allow, deny, created_at, updated_at) "
            "values (?, ?, 'd', 'p:m', ?, ?, ?, ?, ?)",
            [
                ("a1", "a1", '["admin"]', '["tool:*"]', "[]", t, t),
                ("a2", "a2", "[]", '["skill:s"]', '["tool:run_shell"]', t, t),
            ],
        )
        conn.execute(
            "insert into items (kind, name, owner, description, content, enabled, created_at, updated_at) "
            "values ('skill', 's', 'a1', '', 'body', 1, ?, ?)",
            (t, t),
        )
        conn.execute("insert into memories (owner, content, updated_at) values ('a2', 'remember', ?)", (t,))

    migrate(path, BEFORE_UNIFIED_LANGUAGE)

    with sqlite3.connect(path) as conn:
        assert not {"roles", "allow", "deny"} & {r[1] for r in conn.execute("pragma table_info(agents)")}
        # 没有重建：原生 ALTER 加的列还接在原来那一行后面（重建会把每列重新生成在自己的一行）
        ddl = conn.execute("select sql from sqlite_master where name = 'agents'").fetchone()[0]
        assert "NOT NULL, is_default BOOLEAN" in ddl
        assert conn.execute(
            "select agent_id, roles, allow, deny from agent_rules order by agent_id"
        ).fetchall() == [
            ("a1", '["admin"]', '["tool:*"]', "[]"),
            ("a2", "[]", '["skill:s"]', '["tool:run_shell"]'),
        ]
        assert conn.execute("select name, owner from items").fetchall() == [("s", "a1")]
        assert conn.execute("select owner, content from memories").fetchall() == [("a2", "remember")]

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "acd7fd2ea877")
    with sqlite3.connect(path) as conn:
        assert "agent_rules" not in _tables(path)
        assert conn.execute("select owner, content from memories").fetchall() == [("a2", "remember")]
        assert conn.execute("select id, roles, allow, deny from agents order by id").fetchall() == [
            ("a1", '["admin"]', '["tool:*"]', "[]"),
            ("a2", "[]", '["skill:s"]', '["tool:run_shell"]'),
        ]
        assert conn.execute("select name, owner from items").fetchall() == [("s", "a1")]


def test_delegation_parameters_move_to_their_own_table_and_downgrade(tmp_path):
    """#244：session_holds 上委派的 check_seconds / deadline 搬进 delegations（带由关联信件推出的 thread），后台任务的
    占用不搬；两列删掉。降级按 holder 搬回、删表。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, "abea1558f93c")
    t = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', ?, ?)",
            (t, t),
        )
        conn.executemany(
            "insert into sessions (id, agent_id, title, origin, origin_ref, parent_session_id, thread, kind, "
            "created_at, updated_at) values (?, 'a1', 't', ?, NULL, ?, ?, ?, ?, ?)",
            [
                ("p", "user", None, None, "conversation", t, t),
                ("c", "delegation", "p", "review", "thread", t, t),
            ],
        )
        conn.executemany(
            "insert into session_holds (session_id, holder, call_id, awaited, check_seconds, deadline, status, "
            "created_at) values ('p', ?, ?, 0, ?, ?, ?, ?)",
            [
                ("delegation:p:k1", None, 45.0, None, "active", t),
                ("delegation:p:k2", "call", None, "2026-01-01 00:05:00", "released", t),
                ("job:1", None, None, None, "active", t),
            ],
        )
        conn.execute(
            "insert into mail (session_id, kind, content, check_in, attachments, answers, awaited, correlation, "
            "correlation_settled, state, created_at) "
            "values ('c', 'message', 'x', 0, '[]', '{}', 0, 'delegation:p:k1', 0, 'queued', ?)",
            (t,),
        )

    migrate(path, BEFORE_UNIFIED_LANGUAGE)

    with sqlite3.connect(path) as conn:
        assert not {"check_seconds", "deadline"} & {
            r[1] for r in conn.execute("pragma table_info(session_holds)")
        }
        assert conn.execute(
            "select holder, session_id, thread_session_id, check_seconds, deadline, created_at "
            "from delegations order by holder"
        ).fetchall() == [
            ("delegation:p:k1", "p", "c", 45.0, None, t),
            ("delegation:p:k2", "p", None, None, "2026-01-01 00:05:00", t),
        ]

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "abea1558f93c")
    with sqlite3.connect(path) as conn:
        assert "delegations" not in _tables(path)
        assert conn.execute(
            "select holder, check_seconds, deadline from session_holds order by holder"
        ).fetchall() == [
            ("delegation:p:k1", 45.0, None),
            ("delegation:p:k2", None, "2026-01-01 00:05:00"),
            ("job:1", None, None),
        ]


def test_unified_language_renames_columns_marks_the_user_and_downgrades(tmp_path):
    """#246：issue 的创建者与时间线 actor 里用户由 NULL 换成 'user'（两列 NOT NULL、编号不复用），记忆的 owner、
    版本的 author、委派的 session_id 改名，数据不变。降级换回 NULL 与原列名。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, BEFORE_UNIFIED_LANGUAGE)
    t = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', ?, ?)",
            (t, t),
        )
        conn.executemany(
            "insert into issues (id, title, description, status, priority, creator_agent_id, created_at, "
            "updated_at) values (?, 't', '', 'todo', 'none', ?, ?, ?)",
            [(1, None, t, t), (2, "a1", t, t), (3, None, t, t)],
        )
        conn.executemany(
            "insert into issue_activities (id, issue_id, created_at, actor_agent_id, kind) values (?, 1, ?, ?, ?)",
            [
                (1, t, None, "created"),
                (2, t, "a1", "comment"),
                (3, t, "system", "assignee"),
                (4, t, None, "x"),
            ],
        )
        conn.execute("delete from issues where id = 3")
        conn.execute("delete from issue_activities where id = 4")
        conn.executemany(
            "insert into memories (owner, content, updated_at) values (?, ?, ?)",
            [(None, "g", t), ("a1", "m", t)],
        )
        conn.execute(
            "insert into revisions (subject, name, before, after, author, message, created_at) "
            "values ('memory', 'a1', NULL, 'm', 'user', 'x', ?)",
            (t,),
        )
        conn.executemany(
            "insert into sessions (id, agent_id, title, origin, origin_ref, parent_session_id, thread, kind, "
            "created_at, updated_at) values (?, 'a1', 't', ?, NULL, ?, ?, ?, ?, ?)",
            [("p", "user", None, None, "conversation", t, t), ("c", "delegation", "p", "r", "thread", t, t)],
        )
        conn.execute(
            "insert into delegations (holder, session_id, thread_session_id, created_at) "
            "values ('delegation:p:k', 'p', 'c', ?)",
            (t,),
        )

    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id, creator from issues order by id").fetchall() == [
            (1, "user"),
            (2, "a1"),
        ]
        assert conn.execute("select id, actor from issue_activities order by id").fetchall() == [
            (1, "user"),
            (2, "a1"),
            (3, "system"),
        ]
        assert {r[1]: r[3] for r in conn.execute("pragma table_info(issues)")}["creator"] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "insert into issue_activities (issue_id, created_at, actor, kind) values (1, ?, NULL, 'x')",
                (t,),
            )
        # 删掉的编号不复用
        conn.execute(
            "insert into issues (title, description, status, priority, creator, created_at, updated_at) "
            "values ('t', '', 'todo', 'none', 'user', ?, ?)",
            (t, t),
        )
        conn.execute(
            "insert into issue_activities (issue_id, created_at, actor, kind) values (1, ?, 'user', 'x')",
            (t,),
        )
        assert conn.execute("select max(id) from issues").fetchone()[0] == 4
        assert conn.execute("select max(id) from issue_activities").fetchone()[0] == 5
        assert sorted(conn.execute("select agent_id, content from memories"), key=str) == [
            ("a1", "m"),
            (None, "g"),
        ]
        assert conn.execute("select actor from revisions").fetchall() == [("user",)]
        assert conn.execute("select holder, parent_session_id from delegations").fetchall() == [
            ("delegation:p:k", "p")
        ]

    # 外键随列改名：删发起会话时委派随之删除（foreign_keys 只在事务外打开才生效）
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("pragma foreign_keys = on")
    conn.execute("insert into delegations (holder, parent_session_id, created_at) values ('d', 'c', ?)", (t,))
    conn.execute("delete from sessions where id = 'c'")
    assert conn.execute("select holder from delegations").fetchall() == [("delegation:p:k",)]
    conn.close()

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), BEFORE_UNIFIED_LANGUAGE)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id, creator_agent_id from issues order by id").fetchall() == [
            (1, None),
            (2, "a1"),
            (4, None),
        ]
        assert conn.execute("select actor_agent_id from issue_activities order by id").fetchall() == [
            (None,),
            ("a1",),
            ("system",),
            (None,),
        ]
        assert sorted(conn.execute("select owner, content from memories"), key=str) == [
            ("a1", "m"),
            (None, "g"),
        ]
        assert conn.execute("select author from revisions").fetchall() == [("user",)]
        assert "session_id" in {r[1] for r in conn.execute("pragma table_info(delegations)")}


def test_thread_calls_are_backfilled_from_result_metadata_and_delegations_and_downgrade(tmp_path):
    """#251：调用 → thread 的关联从工具结果 metadata 的 muliao_threads 与委派记录回填，按（父会话, 调用 id）去重，
    thread 已删除的不收；降级删表。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, "c2d9e4b7a1f0")
    t = "2026-01-01 00:00:00"

    def result(call_id: str, threads: list[str] | None) -> dict:
        metadata = None if threads is None else {"muliao_attachments": [], "muliao_threads": threads}
        return {
            "part_kind": "tool-return",
            "tool_name": "x",
            "tool_call_id": call_id,
            "content": "ok",
            "metadata": metadata,
        }

    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into agents (id, name, description, model, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', ?, ?)",
            (t, t),
        )
        conn.executemany(
            "insert into sessions (id, agent_id, title, origin, origin_ref, parent_session_id, thread, kind, "
            "created_at, updated_at) values (?, 'a1', 't', ?, NULL, ?, ?, ?, ?, ?)",
            [
                ("p", "user", None, None, "conversation", t, t),
                ("c1", "delegation", "p", "r1", "thread", t, t),
                ("c2", "delegation", "p", "r2", "private_thread", t, t),
            ],
        )
        # delegate_task 的结果带 thread；同一 thread 续写一次；指向已删除 thread 的不收；停止补的结果不带 metadata
        parts = [
            result("d1", ["c1"]),
            result("d2", ["c1"]),
            result("gone", ["deleted"]),
            result("ask2", None),
        ]
        conn.execute(
            "insert into messages (session_id, task_id, payload, created_at) values ('p', 1, ?, ?)",
            (json.dumps({"kind": "request", "parts": parts}), t),
        )
        # 正常回答的 ask 两处都有；被停止的 ask 只在委派记录里；thread 已删除的委派不收
        conn.execute(
            "insert into messages (session_id, task_id, payload, created_at) values ('p', 1, ?, ?)",
            (json.dumps({"kind": "request", "parts": [result("ask1", ["c2"])]}), t),
        )
        conn.executemany(
            "insert into delegations (holder, parent_session_id, thread_session_id, created_at) values (?, 'p', ?, ?)",
            [("delegation:p:ask1", "c2", t), ("delegation:p:ask2", "c2", t), ("delegation:p:ask3", None, t)],
        )

    migrate(path)
    with sqlite3.connect(path) as conn:
        assert sorted(
            conn.execute("select parent_session_id, call_id, thread_session_id from thread_calls")
        ) == [
            ("p", "ask1", "c2"),
            ("p", "ask2", "c2"),
            ("p", "d1", "c1"),
            ("p", "d2", "c1"),
        ]

    # 删 thread 时它的关联随之删除（foreign_keys 只在事务外打开才生效）
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("pragma foreign_keys = on")
    conn.execute("delete from sessions where id = 'c1'")
    assert conn.execute("select call_id from thread_calls order by call_id").fetchall() == [
        ("ask1",),
        ("ask2",),
    ]
    conn.close()

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "c2d9e4b7a1f0")
    assert "thread_calls" not in _tables(path)


def test_todo_issues_become_in_progress_or_waiting_run_ended_gets_origin_and_downgrade(tmp_path):
    """#258：todo 有没完成上游的变 waiting、其余变 in_progress；未投递完的 run_ended 事件补上会话来源；降级 waiting 回 todo。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, "20676599edfd")
    t = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into issues (id, title, description, status, priority, creator, created_at, updated_at) "
            "values (?, 't', '', ?, 'none', 'user', ?, ?)",
            [(i, s, t, t) for i, s in [(1, "todo"), (2, "todo"), (3, "todo"), (4, "done"), (5, "backlog")]],
        )
        # 2 在等没完成的 5；3 的上游 4 已完成
        conn.executemany("insert into issue_dependencies values (?, ?)", [(2, 5), (3, 4)])
        conn.execute(
            "insert into agents (id, name, description, model, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', ?, ?)",
            (t, t),
        )
        conn.execute(
            "insert into sessions (id, agent_id, title, origin, origin_ref, kind, created_at, updated_at) "
            "values ('s', 'a1', 't', 'issue', '1', 'conversation', ?, ?)",
            (t, t),
        )
        payload = {"type": "session.run_ended", "receiver": "r", "id": "m", "data": {"session_id": "s"}}
        conn.executemany(
            "insert into tasks (kind, lane, payload, status, attempts, not_before, created_at) "
            "values ('event', 'l', ?, ?, 0, ?, ?)",
            [(json.dumps(payload), s, t, t) for s in ("pending", "failed", "done")],
        )
        # 会话已删除的不补
        gone = {**payload, "data": {"session_id": "gone"}}
        conn.execute(
            "insert into tasks (kind, lane, payload, status, attempts, not_before, created_at) "
            "values ('event', 'l', ?, 'pending', 0, ?, ?)",
            (json.dumps(gone), t, t),
        )

    migrate(path, "d0f01dc4c372")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id, status from issues order by id").fetchall() == [
            (1, "in_progress"),
            (2, "waiting"),
            (3, "in_progress"),
            (4, "done"),
            (5, "backlog"),
        ]
        data = [json.loads(p)["data"] for (p,) in conn.execute("select payload from tasks order by id")]
        filled = {"session_id": "s", "origin": "issue", "origin_ref": "1"}
        assert data == [filled, filled, {"session_id": "s"}, {"session_id": "gone"}]
        conn.execute(
            "insert into issue_activities (issue_id, created_at, actor, kind) values (1, ?, 'a1', 'unsettled')",
            (t,),
        )

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "20676599edfd")
    with sqlite3.connect(path) as conn:
        assert [s for (s,) in conn.execute("select status from issues order by id")] == [
            "in_progress",
            "todo",
            "in_progress",
            "done",
            "backlog",
        ]
        data = [json.loads(p)["data"] for (p,) in conn.execute("select payload from tasks order by id")]
        assert data == [{"session_id": "s"}] * 3 + [{"session_id": "gone"}]
        assert (
            conn.execute("select count(*) from issue_activities where kind = 'unsettled'").fetchone()[0] == 0
        )


def test_waiting_issues_become_in_progress_and_downgrade_drops_waiting_wake_ups(tmp_path):
    """#261：issue 的 waiting 改成 in_progress（降级不改回）；降级删 wakeups 表前把还在等的唤醒的占用记为 cleared。"""
    from alembic import command

    from muliao.platform.db import _migration_engine

    path = tmp_path / "m.db"
    migrate(path, "d0f01dc4c372")
    t = "2026-01-01 00:00:00"
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into issues (id, title, description, status, priority, creator, created_at, updated_at) "
            "values (?, 't', '', ?, 'none', 'user', ?, ?)",
            [(i, s, t, t) for i, s in [(1, "waiting"), (2, "in_progress"), (3, "blocked")]],
        )

    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select id, status from issues order by id").fetchall() == [
            (1, "in_progress"),
            (2, "in_progress"),
            (3, "blocked"),
        ]
        conn.execute(
            "insert into agents (id, name, description, model, created_at, updated_at) "
            "values ('a1', 'a1', 'd', 'p:m', ?, ?)",
            (t, t),
        )
        conn.execute(
            "insert into sessions (id, agent_id, title, origin, kind, created_at, updated_at) "
            "values ('s', 'a1', 't', 'user', 'conversation', ?, ?)",
            (t, t),
        )
        conn.executemany(
            "insert into session_holds (session_id, holder, awaited, status, created_at) values ('s', ?, 0, ?, ?)",
            [("wake:s:c1", "active", t), ("wake:s:c0", "released", t), ("job:7", "active", t)],
        )
        conn.execute(
            "insert into wakeups (holder, session_id, due_at, note, created_at) values ('wake:s:c1', 's', ?, 'n', ?)",
            (t, t),
        )

    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), "d0f01dc4c372")
    with sqlite3.connect(path) as conn:
        assert [s for (s,) in conn.execute("select status from issues order by id")] == [
            "in_progress",
            "in_progress",
            "blocked",
        ]
        holds = conn.execute(
            "select holder, status, ended_at is not null from session_holds order by id"
        ).fetchall()
        assert holds == [("wake:s:c1", "cleared", 1), ("wake:s:c0", "released", 0), ("job:7", "active", 0)]
        assert conn.execute("select name from sqlite_master where name = 'wakeups'").fetchone() is None
