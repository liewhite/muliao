"""工具调用的展示（#301）：工具挂上的展示规则把参数、结果画成展示块；没规则、参数或结果不合 schema 的显示原文。"""

import logging

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from muliao.platform.tool_views import (
    CodeBlock,
    DiffBlock,
    Link,
    LinksBlock,
    MarkdownBlock,
    TableBlock,
    TerminalBlock,
    ToolRenders,
    truncated,
)
from muliao.sessions.history import interrupted_results
from muliao.sessions.transcript import result_item
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, tool_returns


@pytest.fixture
def renders(container) -> ToolRenders:
    # 装配出的那一份（Toolbox.renders：各工具 metadata 上的规则配上工具自己的参数校验），会话领域用的就是它
    return container.sessions._renders


def _term(command=None, output=None, status=None, failed=False) -> TerminalBlock:
    return TerminalBlock(command=command, output=output, status=status, failed=failed)


async def test_shell_results_show_exit_code_timeout_and_background_job(renders):
    shell = renders["run_shell"]
    assert shell.call_view({"command": "ls", "timeout": 5}) == [_term(command="ls")]
    assert shell.result_view("exit_code: 0\nhi\n") == [_term(output="hi\n", status="exit 0")]
    assert shell.result_view("exit_code: 2\nboom") == [_term(output="boom", status="exit 2", failed=True)]
    assert shell.result_view("killed after 5.0s timeout\npartial") == [
        _term(output="partial", status="killed after 5.0s timeout", failed=True)
    ]
    started = "started background job 7; output goes to /w/jobs/7.log.\nYou will get a check-in event"
    assert shell.result_view(started) == [
        _term(status="started background job 7; output goes to /w/jobs/7.log.")
    ]
    # 认不出的结果显示原文
    assert shell.result_view("something else") is None


async def test_calls_rejected_by_the_tool_schema_are_shown_raw(renders):
    assert renders["run_shell"].call_view({"cmd": "ls"}) is None
    assert renders["edit_file"].call_view({"path": "a", "edits": "nope"}) is None
    # 合 schema 但缺定位方式、缺路径（工具自己会拒绝）：不画无意义的块
    assert renders["edit_file"].call_view({"path": "a", "new_text": "x"}) is None
    assert renders["read_file"].call_view({}) is None


async def test_structured_results_that_do_not_fit_their_type_are_shown_raw_and_logged(renders, caplog):
    with caplog.at_level(logging.WARNING):
        assert renders["issue_list"].result_view('[{"id": "x"}]') is None
    assert any(getattr(r, "tool", None) == "issue_list" for r in caplog.records)


async def test_edit_file_shows_each_change_as_a_diff(renders):
    view = renders["edit_file"].call_view(
        {
            "path": "a.py",
            "edits": [
                {"old_text": "x = 1\ny = 2", "new_text": "x = 1\ny = 3"},
                {"path": "b.py", "start_line": 3, "end_line": 4, "anchor": "z", "new_text": "w"},
            ],
        }
    )
    assert view == [
        DiffBlock(title="a.py", text="@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3"),
        DiffBlock(title="b.py · lines 3-4", text="+w"),
    ]
    assert renders["edit_file"].call_view({"path": "a.py", "old_text": "a", "new_text": "b"}) == [
        DiffBlock(title="a.py", text="@@ -1 +1 @@\n-a\n+b")
    ]


async def test_read_file_lists_the_parts_and_shows_the_content_as_code(renders):
    read = renders["read_file"]
    assert read.call_view({"reads": [{"path": "a"}, {"path": "b", "offset": 10, "limit": 5}]}) == [
        CodeBlock(title=None, text="a\nb  (lines 10-14)")
    ]
    assert read.result_view("     1\tx") == [CodeBlock(title=None, text="     1\tx")]


async def test_issue_list_is_a_table(renders):
    text = (
        '[{"id":1,"title":"t","owner":"o","status":"backlog","priority":"none","assignee":null,'
        '"assignee_state":null,"stalled":false,"depends_on":[],"waiting_on":[],"updated_at":"2026-10-29T00:00:00Z"}]'
    )
    assert renders["issue_list"].result_view(text) == [
        TableBlock(
            columns=["#", "title", "status", "assignee", "owner"], rows=[["#1", "t", "backlog", "", "o"]]
        )
    ]


async def test_issue_create_shows_each_issue_of_a_batch(renders):
    view = renders["issue_create"].call_view(
        {
            "issues": [
                {"title": "a", "owner": "o", "description": "d"},
                {"title": "b", "owner": "o", "key": "k"},
            ]
        }
    )
    assert view is not None
    assert [b.kind for b in view] == ["fields", "markdown", "fields"]


async def test_results_made_up_on_interruption_are_shown_raw(renders):
    call = ToolCallPart("run_shell", {"command": "sleep 9", "timeout": 60}, tool_call_id="c")
    part = interrupted_results([call])[0].parts[0]
    assert isinstance(part, ToolReturnPart)
    assert result_item(part, renders).view is None


# 每个挂了规则的工具一份最小的合法参数：规则按参数名取值，写错名字会直接报错（规则内部的错误不兜底）
_MINIMAL_ARGS = {
    "run_shell": {"command": "ls", "timeout": 5},
    "read_file": {"path": "a"},
    "write_file": {"path": "a", "content": "x"},
    "edit_file": {"path": "a", "old_text": "x", "new_text": "y"},
    "issue_list": {},
    "issue_show": {"issue_id": 1},
    "issue_create": {"title": "t", "owner": "o", "description": "d"},
    "issue_update": {"issue_id": 1, "status": "done", "unassign": True},
    "issue_stop": {"issue_id": 1, "status": "cancelled", "message": "m"},
    "issue_comment": {"issue_id": 1, "body": "b"},
    "notify_user": {"title": "t", "body": "b"},
    "read_skill": {"name": "s"},
    "write_skill": {"name": "s", "description": "d", "content": "c", "reason": "r"},
    "edit_skill": {"name": "s", "reason": "r", "old_text": "a", "new_text": "b"},
    "memory_edit": {"new_text": "n", "reason": "r"},
    "memory_edit_public": {"new_text": "n", "reason": "r", "old_text": "o"},
}


async def test_every_render_draws_its_tool_s_minimal_call(renders):
    assert set(renders) == set(_MINIMAL_ARGS)
    for name, args in _MINIMAL_ARGS.items():
        assert renders[name].call_view(args) is not None, name


async def test_web_renders():
    from muliao.agent_runtime.toolbox import Toolbox
    from muliao.web.tools import web_toolset

    class NoWeb:
        async def search(self, query, limit):
            raise AssertionError

        async def fetch(self, url):
            raise AssertionError

    async def no_rules(_agent_id):
        raise AssertionError

    web = Toolbox(toolsets=[web_toolset(NoWeb(), 1000)], mcp=lambda: None, rules_of=no_rules).renders()  # type: ignore[arg-type]
    assert web["web_fetch"].call_view({"url": "https://x"}) is not None
    assert web["web_fetch"].result_view("# page") is None
    hits = '[{"title":"T","url":"https://x","summary":"s","site_name":null,"published":null}]'
    assert web["web_search"].call_view({"query": "q"}) is not None
    assert web["web_search"].result_view(hits) == [
        LinksBlock(links=[Link(title="T", url="https://x", summary="s")])
    ]


def test_live_previews_truncate_the_text_inside_blocks():
    [block] = truncated([MarkdownBlock(title="t", text="x" * 50)], 10)
    assert isinstance(block, MarkdownBlock) and len(block.text) < 50 and block.title == "t"


async def test_the_session_page_shows_rendered_calls_and_raw_ones(client, container, make_agent, models):
    await login(client)
    agent = await make_agent("worker", model="shell")

    def fn(messages: list[ModelMessage], info) -> ModelResponse:
        if tool_returns(messages):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[
                ToolCallPart("run_shell", {"command": "echo hi", "timeout": 30}, tool_call_id="s"),
                ToolCallPart("list_agents", {}, tool_call_id="l"),
            ]
        )

    models.models["shell"] = scripted(fn)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "go"})).json()[
        "id"
    ]
    await drain(container)

    items = (await client.get(f"/api/sessions/{session_id}")).json()["items"]
    by_call = {(i["tool_call_id"], i["type"]): i["view"] for i in items if i["tool_call_id"]}
    assert by_call[("s", "tool_call")] == [
        {"kind": "terminal", "command": "echo hi", "output": None, "status": None, "failed": False}
    ]
    assert by_call[("s", "tool_result")] == [
        {"kind": "terminal", "command": None, "output": "hi\n", "status": "exit 0", "failed": False}
    ]
    # 没定展示规则的工具显示原文
    assert by_call[("l", "tool_call")] is None and by_call[("l", "tool_result")] is None
