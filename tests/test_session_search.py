"""会话按关键字搜索（#254）：消息的可搜索文字（sessions.history.searchable_text）与回填它的迁移。"""

import sqlite3

from alembic import command
from pydantic_ai.messages import (
    ImageUrl,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from muliao.platform.db import _migration_engine, alembic_config, migrate
from muliao.sessions.codec import dump_message
from muliao.sessions.conventions import attach_run_context
from muliao.sessions.history import searchable_text

BEFORE_MESSAGE_TEXTS = "29411a27d5ec"


def _messages():
    request = ModelRequest(
        parts=[
            ToolReturnPart(tool_name="read_file", content="file body usage", tool_call_id="c0"),
            UserPromptPart(["看看 部署", ImageUrl(url="https://example.com/a.png")]),
        ]
    )
    attach_run_context(request, "[Current local time: 2026-10-27]")
    response = ModelResponse(
        parts=[
            ThinkingPart("secret thoughts"),
            TextPart("好的"),
            ToolCallPart("run_shell", '{"command": "echo \\"hi\\" && a\\\\b", "timeout": 30}', "c1"),
            ToolCallPart("edit_file", {"edits": [{"old_text": "x", "new_text": "y"}]}, "c2"),
            ToolCallPart("write_file", '{"path": "broken', "c3"),
        ]
    )
    return [request, response, ModelRequest(parts=[ToolReturnPart("run_shell", "only output", "c1")])]


def test_searchable_text_keeps_what_was_said_and_called_but_not_results_thoughts_or_run_context():
    request, response, results = _messages()
    assert searchable_text(request) == "看看 部署"
    # 参数按值解开（JSON 转义不再挡住引号、反斜杠），不含键名；不是合法 JSON 的参数用原文
    assert searchable_text(response).split("\n") == [
        "好的",
        "run_shell",
        'echo "hi" && a\\b',
        "edit_file",
        "x",
        "y",
        "write_file",
        '{"path": "broken',
    ]
    assert searchable_text(results) == ""


def test_message_texts_are_backfilled_like_new_messages_and_dropped_on_downgrade(tmp_path):
    path = tmp_path / "m.db"
    migrate(path)
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), BEFORE_MESSAGE_TEXTS)
    messages = _messages()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into sessions (id, agent_id, title, origin, kind, created_at, updated_at) "
            "values ('s1', 'a1', 't', 'user', 'chat', '2026-01-01', '2026-01-01')"
        )
        for i, m in enumerate(messages, start=1):
            conn.execute(
                "insert into messages (id, session_id, task_id, payload, created_at) values (?, 's1', 1, ?, '2026-01-01')",
                (i, dump_message(m)),
            )
    migrate(path)
    with sqlite3.connect(path) as conn:
        rows = dict(conn.execute("select message_id, text from message_texts"))
    # 没有文字的消息（只有工具结果）没有这一行
    assert rows == {1: searchable_text(messages[0]), 2: searchable_text(messages[1])}
    with _migration_engine(path).begin() as connection:
        command.downgrade(alembic_config(connection), BEFORE_MESSAGE_TEXTS)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select name from sqlite_master where name = 'message_texts'").fetchall() == []
        assert conn.execute("select count(*) from messages").fetchone() == (3,)
