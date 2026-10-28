"""issue_stop（#272）：叫停被指派 agent 在 issue 上的会话——停掉它（同用户停止），改状态，时间线记两条；有消息时停完
投进那个会话跑一轮，不论新状态。"""

import asyncio

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from muliao.agents.api import USER
from muliao.errors import Conflict, InvalidInput
from muliao.issues.api import ActivityKind, IssueStatus
from muliao.sessions.conventions import STOPPED_TOOL_RESULT
from muliao.sessions.tables import MailState
from tests.scripted import scripted
from tests.test_flow import deliver, drain, history_of, mail_of
from tests.test_issues import _issue_sessions, _waits

BACKLOG, PROGRESS = IssueStatus.BACKLOG, IssueStatus.IN_PROGRESS


class Worker:
    """开工就跑一条慢命令；收到带「wrap up」或「instead」的信时记下信的内容并收尾（约一个唤醒，免得被请表态）。"""

    def __init__(self) -> None:
        self.letters: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        prompt = (
            " ".join(str(p.content) for p in last.parts if isinstance(p, UserPromptPart))
            if isinstance(last, ModelRequest)
            else ""
        )
        if "wrap up" in prompt or "instead" in prompt:
            self.letters.append(prompt)
            return ModelResponse(
                parts=[ToolCallPart("wake_me", {"after_seconds": 3600, "note": "later"}, tool_call_id="w")]
            )
        if any(isinstance(p, ToolCallPart) for m in messages for p in getattr(m, "parts", [])):
            return ModelResponse(parts=[TextPart("ok")])
        return ModelResponse(
            parts=[ToolCallPart("run_shell", {"command": "sleep 30", "timeout": 60}, tool_call_id="slow")]
        )


async def _wait_for(predicate) -> None:
    for _ in range(500):
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def _running_issue(container, make_agent, models):
    """alpha 在 issue 会话里跑着一条慢命令；返回 (alpha id, boss id, issue, 会话, 模型脚本, drain 任务)。"""
    script = Worker()
    models.models["worker"] = scripted(script)
    alpha = (await make_agent("alpha", model="worker")).id
    boss = (await make_agent("boss")).id
    issue = await container.issues.create(USER, owner=boss, title="t", status=PROGRESS, assignee=alpha)
    worker = asyncio.create_task(drain(container, limit_seconds=20))

    async def slow_running() -> bool:
        sessions = await _issue_sessions(container)
        if not sessions:
            return False
        live = container.live.snapshot(sessions[0].id)
        return live is not None and any(
            e.event == "item" and e.item.tool_call_id == "slow" for e in live.events
        )

    await _wait_for(slow_running)
    [session] = await _issue_sessions(container)
    return alpha, boss, issue, session, script, worker


async def _kinds(container, issue_id: int) -> list[ActivityKind]:
    return [a.kind for a in (await container.issues.get(issue_id)).activities]


async def test_stopping_a_running_issue_conversation_then_delivers_the_message(container, make_agent, models):
    alpha, boss, issue, session, script, worker = await _running_issue(container, make_agent, models)

    result = await container.issues.stop(boss, issue.id, session_id=None, status=BACKLOG, message="wrap up")
    await worker
    await drain(container)

    assert result.stopped and result.agent == alpha and result.issue.status == BACKLOG
    # 时间线：一条停止、一条状态变更，消息记成评论
    activities = (await container.issues.get(issue.id)).activities
    assert [a.kind for a in activities][-3:] == [
        ActivityKind.STOPPED,
        ActivityKind.STATUS,
        ActivityKind.COMMENT,
    ]
    assert activities[-3].actor == boss and activities[-3].change == {"from": None, "to": alpha}
    # 慢命令被停下（同用户停止），之后消息投进同一个会话跑了一轮，虽然 issue 已是 backlog
    history = await history_of(container, session.id)
    assert any(getattr(p, "content", None) == STOPPED_TOOL_RESULT for m in history for p in m.parts)
    [letter] = script.letters
    assert "boss stopped alpha's conversation on this issue" in letter
    assert "boss changed status: in_progress -> backlog" in letter and "wrap up" in letter
    assert [s.id for s in await _issue_sessions(container)] == [session.id]


async def test_redirecting_keeps_the_issue_in_progress(container, make_agent, models):
    _alpha, boss, issue, session, script, worker = await _running_issue(container, make_agent, models)

    await container.issues.stop(boss, issue.id, session_id=None, status=PROGRESS, message="do X instead")
    await worker
    await drain(container)

    assert (await container.issues.get(issue.id)).issue.status == PROGRESS
    # 状态没变：时间线只有停止与评论
    assert (await _kinds(container, issue.id))[-2:] == [ActivityKind.STOPPED, ActivityKind.COMMENT]
    [letter] = script.letters
    assert "do X instead" in letter


async def test_stopping_without_a_message_clears_the_mailbox_and_delivers_nothing(container, make_agent):
    alpha, boss = (await make_agent("alpha")).id, (await make_agent("boss")).id
    issue = await container.issues.create(USER, owner=boss, title="t", status=PROGRESS, assignee=alpha)
    await deliver(container)
    [session] = await _issue_sessions(container)

    # 信还排着、会话没在运行：停止就是清空信箱
    result = await container.issues.stop(
        boss, issue.id, session_id=None, status=IssueStatus.CANCELLED, message=None
    )
    await drain(container)

    assert result.stopped
    assert [m.state for m in await mail_of(container) if m.session_id == session.id] == [MailState.DISCARDED]
    assert (await _kinds(container, issue.id))[-2:] == [ActivityKind.STOPPED, ActivityKind.STATUS]
    assert (await container.issues.get(issue.id)).issue.status == IssueStatus.CANCELLED


async def test_an_assignee_without_a_conversation_only_gets_the_status_changed(container, make_agent):
    alpha, boss = (await make_agent("alpha")).id, (await make_agent("boss")).id
    issue = await container.issues.create(USER, owner=boss, title="t", assignee=alpha)

    result = await container.issues.stop(
        boss, issue.id, session_id=None, status=IssueStatus.BLOCKED, message="hold"
    )
    await deliver(container)

    assert not result.stopped and result.issue.status == IssueStatus.BLOCKED
    assert (await _kinds(container, issue.id))[-2:] == [ActivityKind.STATUS, ActivityKind.COMMENT]
    # 没有会话可收尾：不为它新建会话（blocked 按规则不投）
    assert await _issue_sessions(container) == []


async def test_stop_is_refused_where_it_makes_no_sense(container, make_agent):
    alpha, boss = (await make_agent("alpha")).id, (await make_agent("boss")).id
    issue = await container.issues.create(USER, owner=boss, title="t", status=PROGRESS, assignee=alpha)
    await deliver(container)
    [session] = await _issue_sessions(container)

    for status in (IssueStatus.IN_REVIEW, IssueStatus.DONE):
        with pytest.raises(InvalidInput, match="stopping is not finishing"):
            await container.issues.stop(boss, issue.id, session_id=None, status=status, message=None)
    with pytest.raises(InvalidInput, match="give a message"):
        await container.issues.stop(boss, issue.id, session_id=None, status=PROGRESS, message=None)
    # 在这个 issue 自己的会话里叫停自己
    with pytest.raises(InvalidInput, match="conversation of issue"):
        await container.issues.stop(alpha, issue.id, session_id=session.id, status=BACKLOG, message=None)
    unassigned = await container.issues.create(USER, owner=boss, title="u", status=PROGRESS)
    with pytest.raises(Conflict, match="not assigned to an agent"):
        await container.issues.stop(boss, unassigned.id, session_id=None, status=BACKLOG, message=None)
    # 都没有动 issue
    assert (await container.issues.get(issue.id)).issue.status == PROGRESS
    assert ActivityKind.STOPPED not in await _kinds(container, issue.id)


async def test_issue_stop_tool(container, make_agent, models):
    from tests.test_tools import run_tools

    models.models["test"] = scripted(_waits)
    me = await make_agent("manager", model="tools")
    alpha = (await make_agent("alpha")).id
    issue = await container.issues.create(USER, owner=me.id, title="t", status=PROGRESS, assignee=alpha)
    other = await container.issues.create(USER, owner=me.id, title="o", assignee=alpha)
    await deliver(container)
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("issue_stop", {"issue_id": issue.id, "status": "cancelled"}),
            ("issue_stop", {"issue_id": other.id, "status": "backlog"}),
            ("issue_stop", {"issue_id": other.id, "status": "done"}),
        ],
    )
    assert outputs[0] == f"stopped alpha's conversation on issue #{issue.id}; it is cancelled"
    assert (
        outputs[1]
        == f"alpha has no conversation on issue #{other.id} yet, nothing to stop; issue #{other.id} is backlog"
    )
    assert "stopping is not finishing" in outputs[2]
