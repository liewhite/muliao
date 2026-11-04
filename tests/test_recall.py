"""热历史（#368）：activity_overview 列出某天的会话、用户说的话（只按信件的发送方认）、失败的运行与工作历史；
session_read 读一个会话的时间线，标出谁说的、截断、过滤与分页。"""

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents.ids import USER
from muliao.history.service import HistoryEntry
from muliao.sessions.api import Origin
from tests.scripted import scripted
from tests.test_flow import drain, reply, start
from tests.test_tools import run_tools


def _last_prompt(messages: list[ModelMessage]) -> str:
    request = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
    texts = [
        c
        for p in request.parts
        if isinstance(p, UserPromptPart)
        for c in ([p.content] if isinstance(p.content, str) else p.content)
        if isinstance(c, str)
    ]
    return texts[-1]


def _worker(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if "explode" in _last_prompt(messages):
        raise ModelHTTPError(status_code=400, model_name="w", body="bad request")
    return ModelResponse(parts=[TextPart("ok")])


async def _day(container, make_agent, models):
    """worker 当天：一个触发器会话（用户在里面纠正了一句）、一个用户会话的运行失败；另一个 agent 也有会话；worker 记了一条历史。"""
    models.models["w"] = scripted(_worker)
    worker = await make_agent("worker", model="w")
    other = await make_agent("other", model="w")
    nightly = await start(container, worker.id, "nightly job", origin=Origin.TRIGGER)
    await drain(container)
    await reply(container, nightly.id, "不对，要先备份")
    await drain(container)
    await start(container, worker.id, "explode now")
    await start(container, other.id, "别人的事", origin=Origin.TRIGGER)
    await drain(container)
    # 用户在 worker 负责的 issue 上评论（投进 worker 的 issue 会话；那封信不算用户在会话里说的话）
    issue = await container.issues.create(USER, owner=worker.id, title="修备份")
    await container.issues.comment(USER, issue.id, "备份要保留七天\n第二行")
    await drain(container)
    today = container.history.today()
    await container.history.append(worker.id, [HistoryEntry(today, "备份了数据库", "做完", ("#1",), 2)])
    return worker, nightly


async def test_overview_lists_sessions_what_the_user_typed_failures_and_history(
    container, make_agent, models
):
    worker, nightly = await _day(container, make_agent, models)
    me = await make_agent("me", model="tools")
    mine, everyone, long_ago = await run_tools(
        container,
        models,
        me.id,
        [
            ("activity_overview", {"agent": "worker"}),
            ("activity_overview", {"agent": "all"}),
            ("activity_overview", {"agent": "worker", "day": "2000-01-01"}),
        ],
    )

    sessions, typed, on_issues, failed, history = mine.split("\n## ")[1:6]
    assert sessions.startswith("sessions (3, issue 1, trigger 1, user 1)")
    assert f"{nightly.id[:8]} [trigger] 'nightly job': idle," in sessions
    assert "'explode now': idle, 1 msgs" in sessions and "1 runs, 1 FAILED" in sessions
    assert "别人的事" not in mine
    # 用户说的话只有用户在界面上发的；触发器的来信不算
    assert typed.startswith("what the user typed (2, full text)")
    assert "不对，要先备份" in typed and "explode now" in typed and "\n  nightly job" not in typed
    assert "New activity on issue" not in typed
    assert on_issues.startswith("what the user did on issues (1 issues, comments in full)")
    assert "'修备份' (now backlog):" in on_issues and "created the issue" in on_issues
    assert "commented:\n    备份要保留七天\n    第二行" in on_issues
    assert failed.startswith("failed runs (1)") and "400" in failed
    assert "[2] 备份了数据库 ['#1']" in history
    # 全部 agent：带上 agent 名
    assert "other " in everyone and "别人的事" in everyone and "worker " in everyone
    assert "sessions (0)" in long_ago and "(none yet)" in long_ago


async def test_session_read_labels_who_said_what_and_filters(container, make_agent, models):
    worker, nightly = await _day(container, make_agent, models)
    me = await make_agent("me", model="tools")
    full, grepped, unknown, ambiguous, bad_regex = await run_tools(
        container,
        models,
        me.id,
        [
            ("session_read", {"session": nightly.id[:8]}),
            ("session_read", {"session": nightly.id, "grep": "备份"}),
            ("session_read", {"session": "zzzz"}),
            ("session_read", {"session": ""}),
            ("session_read", {"session": nightly.id, "grep": "("}),
        ],
    )

    assert f"# session {nightly.id} [trigger] 'nightly job', agent worker: 4 entries" in full
    assert "] TRIGGER\n  nightly job" in full
    assert "] USER\n  不对，要先备份" in full
    assert full.count("] ASSISTANT\n  ok") == 2
    assert "1 entries matching /备份/" in grepped and "nightly job\n" not in grepped.split("\n", 1)[1]
    assert "no session id starts with 'zzzz'" in unknown
    assert "matches several sessions" in ambiguous
    assert "not a valid regular expression" in bad_regex


async def test_session_read_marks_only_the_users_own_messages_in_an_issue_session(
    container, make_agent, models
):
    """用户在 issue 会话里直接回过一句「备份」：之后 issue 通知里含有这两个字的不算用户说的（只认同一次运行处理的用户信件、
    整段对上）。"""
    worker, _ = await _day(container, make_agent, models)
    [listed] = await container.sessions.list(agent_id=worker.id, origins=[Origin.ISSUE], limit=5, offset=0)
    await reply(container, listed.session.id, "备份")
    await drain(container)
    [issue] = await container.issues.list(owner=worker.id)
    await container.issues.comment(USER, issue.id, "备份再确认一下")
    await drain(container)
    me = await make_agent("me", model="tools")
    (read,) = await run_tools(container, models, me.id, [("session_read", {"session": listed.session.id})])

    assert read.count("] ISSUE\n  New activity on issue") == 2
    assert read.count("] USER\n") == 1 and "] USER\n  备份\n" in read


async def test_session_read_clips_tool_output_and_pages_at_the_output_limit(container, make_agent, models):
    models.models["w"] = scripted(lambda messages, info: ModelResponse(parts=[TextPart("ok")]))
    worker = await make_agent("worker", model="w")
    limit = container.settings.runtime.tool_output_max_chars
    long = await start(container, worker.id, "a" * (limit // 2 + 10))
    await drain(container)
    await reply(container, long.id, "b" * (limit // 2 + 10))
    await drain(container)
    me = await make_agent("me", model="tools")
    first, rest, past = await run_tools(
        container,
        models,
        me.id,
        [
            ("session_read", {"session": long.id}),
            ("session_read", {"session": long.id, "offset": 3}),
            ("session_read", {"session": long.id, "offset": 9}),
        ],
    )
    assert "a" * 100 in first and "b" * 100 not in first
    assert "[output limit reached at entry 2 of 4; continue with offset=3]" in first
    assert "b" * 100 in rest and "output limit" not in rest
    assert "offset 9 is past the last entry (4)" in past

    # 活动概览也按同一个上限在整行处停下，按提示的 offset 接着读
    (first_page,) = await run_tools(container, models, me.id, [("activity_overview", {"agent": "worker"})])
    assert "a" * 100 in first_page and "b" * 100 not in first_page
    note = first_page.splitlines()[-1]
    assert note.startswith("[output limit reached at line ")
    offset = int(note.rsplit("offset=", 1)[1].rstrip("]"))
    (next_page,) = await run_tools(
        container, models, me.id, [("activity_overview", {"agent": "worker", "offset": offset})]
    )
    assert "b" * 100 in next_page

    # 工具结果按 clip 截断；用户与助手的文字不截
    calls = [("session_read", {"session": long.id, "clip": 5})]
    (clipped,) = await run_tools(container, models, me.id, calls)
    (reread,) = await run_tools(
        container, models, me.id, [("session_read", {"session": (await _session_of(container, me.id))[:8]})]
    )
    assert "a" * 100 in clipped
    assert "more chars]" in reread


async def _session_of(container, agent_id: str) -> str:
    """agent 最近开的会话（run_tools 每次新开一个触发器会话）。"""
    listed = await container.sessions.list(agent_id=agent_id, origins=None, limit=1, offset=0)
    return listed[0].session.id
