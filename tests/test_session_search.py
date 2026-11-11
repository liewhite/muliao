"""会话按关键字搜索（#254）：消息的可搜索文字（sessions.history.searchable_text）。"""

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

from muliao.sessions.conventions import attach_run_context
from muliao.sessions.history import searchable_text


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
