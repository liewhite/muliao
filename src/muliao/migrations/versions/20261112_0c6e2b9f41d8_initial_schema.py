"""起点：#644 把此前 48 个迁移压成这一个，结构即当时线上库删掉 #498 之前的旧表与 agents 的旧配置列之后的样子。

Revision ID: 0c6e2b9f41d8
Revises:
Create Date: 2026-11-12 00:18:21.541136
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0c6e2b9f41d8"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("importance", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("agent_history", schema=None) as batch_op:
        batch_op.create_index("ix_agent_history_agent_day", ["agent_id", "day"], unique=False)

    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("deleting_by", sa.String(length=64), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.create_index(
            "uq_agents_default", ["is_default"], unique=True, sqlite_where=sa.text("is_default")
        )

    op.create_table(
        "files",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "issues",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("assignee", sa.String(length=32), nullable=True),
        sa.Column("owner_agent_id", sa.String(length=32), nullable=True),
        sa.Column("creator", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("issues", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_issues_assignee"), ["assignee"], unique=False)
        batch_op.create_index(batch_op.f("ix_issues_owner_agent_id"), ["owner_agent_id"], unique=False)
        batch_op.create_index("ix_issues_status", ["status"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("secrets", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_path", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("check_seconds", sa.Double(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_jobs_agent_id"), ["agent_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_session_id"), ["session_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_status"), ["status"], unique=False)

    op.create_table(
        "model_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("model_usage", schema=None) as batch_op:
        batch_op.create_index("ix_model_usage_created_at", ["created_at"], unique=False)
        batch_op.create_index("ix_model_usage_session_id", ["session_id"], unique=False)

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("unread", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notifications_issue_id"), ["issue_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_session_id"), ["session_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_unread"), ["unread"], unique=False)

    op.create_table(
        "push_subscriptions",
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("endpoint"),
    )
    op.create_table(
        "script_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("skill", sa.String(length=100), nullable=False),
        sa.Column("script", sa.Text(), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("secrets", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("script_grants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_script_grants_agent_id"), ["agent_id"], unique=False)

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("origin_ref", sa.String(length=64), nullable=True),
        sa.Column("parent_session_id", sa.String(length=36), nullable=True),
        sa.Column("thread", sa.String(length=100), nullable=True),
        sa.Column("compaction_threshold_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_session_id"], ["sessions.id"], name="fk_sessions_parent_session_id", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sessions_agent_id"), ["agent_id"], unique=False)
        batch_op.create_index("ix_sessions_origin_ref", ["origin", "origin_ref"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_sessions_parent_session_id"), ["parent_session_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_sessions_updated_at"), ["updated_at"], unique=False)
        batch_op.create_index(
            "uq_sessions_thread",
            ["parent_session_id", "agent_id", "thread"],
            unique=True,
            sqlite_where=sa.text("parent_session_id IS NOT NULL"),
        )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("lane", sa.String(length=100), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("not_before", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tasks_agent_id"), ["agent_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tasks_session_id"), ["session_id"], unique=False)
        batch_op.create_index("ix_tasks_status_lane", ["status", "lane"], unique=False)

    op.create_table(
        "triggers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(), nullable=True),
        sa.Column("last_target", sa.JSON(), nullable=True),
        sa.Column("route", sa.JSON(), nullable=True),
        sa.Column("precheck", sa.JSON(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("precheck_output", sa.Text(), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("faults", sa.JSON(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("triggers", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_triggers_agent_id"), ["agent_id"], unique=False)
        batch_op.create_index(
            "uq_triggers_name_unfinished", ["name"], unique=True, sqlite_where=sa.text("finished_at IS NULL")
        )

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=100), nullable=False),
        sa.Column("pin", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("approval_requests", schema=None) as batch_op:
        batch_op.create_index("uq_approval_requests_call", ["session_id", "call_id"], unique=True)

    op.create_table(
        "compactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("through_message_id", sa.Integer(), nullable=False),
        sa.Column("at_message_id", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("compactions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_compactions_session_id"), ["session_id"], unique=False)

    op.create_table(
        "issue_activities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("change", sa.JSON(), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("issue_activities", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_issue_activities_issue_id"), ["issue_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_issue_activities_session_id"), ["session_id"], unique=False)

    op.create_table(
        "issue_dependencies",
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("depends_on_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["depends_on_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("issue_id", "depends_on_id"),
    )
    with op.batch_alter_table("issue_dependencies", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_issue_dependencies_depends_on_id"), ["depends_on_id"], unique=False
        )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_messages_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_messages_session_id"), ["session_id"], unique=False)

    op.create_table(
        "thread_calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_session_id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=100), nullable=False),
        sa.Column("thread_session_id", sa.String(length=36), nullable=False),
        sa.Column("check_seconds", sa.Double(), nullable=True),
        sa.ForeignKeyConstraint(["parent_session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["thread_session_id"],
            ["sessions.id"],
            name="fk_thread_calls_thread_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("thread_calls", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_thread_calls_thread_session_id"), ["thread_session_id"], unique=False
        )
        batch_op.create_index("uq_thread_calls_call", ["parent_session_id", "call_id"], unique=True)

    op.create_table(
        "wakeups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("holder", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("wakeups", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_wakeups_session_id"), ["session_id"], unique=False)
        batch_op.create_index("uq_wakeups_holder", ["holder"], unique=True)

    op.create_table(
        "mail",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("check_in", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("awaited", sa.Boolean(), nullable=False),
        sa.Column("delegation_id", sa.Integer(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("taken_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["delegation_id"], ["thread_calls.id"], name="fk_mail_delegation_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["taken_by"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("mail", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mail_created_at"), ["created_at"], unique=False)
        batch_op.create_index(
            "ix_mail_delegation_id",
            ["delegation_id"],
            unique=False,
            sqlite_where=sa.text("delegation_id IS NOT NULL"),
        )
        batch_op.create_index(
            "ix_mail_queued", ["session_id"], unique=False, sqlite_where=sa.text("state = 'queued'")
        )
        batch_op.create_index(batch_op.f("ix_mail_session_id"), ["session_id"], unique=False)
        batch_op.create_index(
            "ix_mail_taken_by", ["taken_by"], unique=False, sqlite_where=sa.text("taken_by IS NOT NULL")
        )
        batch_op.create_index(
            "uq_mail_dedupe_key",
            ["session_id", "dedupe_key"],
            unique=True,
            sqlite_where=sa.text("dedupe_key IS NOT NULL"),
        )

    op.create_table(
        "message_texts",
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_table(
        "session_holds",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("holder", sa.String(length=200), nullable=True),
        sa.Column("thread_call_id", sa.Integer(), nullable=True),
        sa.Column("call_id", sa.String(length=100), nullable=True),
        sa.Column("awaited", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["thread_call_id"],
            ["thread_calls.id"],
            name="fk_session_holds_thread_call_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("session_holds", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_session_holds_session_id"), ["session_id"], unique=False)
        batch_op.create_index("uq_session_holds_holder", ["holder"], unique=True)
        batch_op.create_index(
            "uq_session_holds_thread_call_id",
            ["thread_call_id"],
            unique=True,
            sqlite_where=sa.text("thread_call_id IS NOT NULL"),
        )

    # 工作历史的全文检索（muliao.history.tables.FTS_TABLE，不在 metadata 里）与只增不改的约束
    op.execute("CREATE VIRTUAL TABLE agent_history_fts USING fts5(title, summary, refs, tokenize='trigram')")
    op.execute(
        "CREATE TRIGGER agent_history_fts_insert AFTER INSERT ON agent_history BEGIN "
        "INSERT INTO agent_history_fts(rowid, title, summary, refs) VALUES (new.id, new.title, new.summary, "
        "(SELECT group_concat(value, ' ') FROM json_each(new.refs))); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER agent_history_no_update BEFORE UPDATE ON agent_history BEGIN "
        "SELECT RAISE(ABORT, 'agent_history is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER agent_history_no_delete BEFORE DELETE ON agent_history BEGIN "
        "SELECT RAISE(ABORT, 'agent_history is append-only'); END"
    )


def downgrade() -> None:
    # 起点之前没有可回的结构：回滚到 #644 之前的版本要连数据库一起从备份恢复
    raise NotImplementedError("initial schema has no downgrade; restore the database from a backup")
