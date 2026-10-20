"""delegate_task(report_back)：thread 的结果（完成、被停止、最终失败）作为事件交回发起委派的会话。"""

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.models import ReplyTo, SessionEvent, TaskKind, TaskStatus
from muliao.runtime.conversation import ReportTo, Threads
from tests.scripted import scripted
from tests.test_events import Recorder
from tests.test_flow import drain, start, tasks_of
from tests.test_threads import call, echo, steps, threads_of


async def test_a_reported_thread_answer_comes_back_as_an_event_not_a_push(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("reviewer", model="v")
    models.models["v"] = echo("reviewed")

    calls = {"n": 0}

    def lead_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        recorder(messages, info)
        if calls["n"] == 1:
            return call(
                "delegate_task",
                {"agent": "reviewer", "task": "check v1", "thread": "review", "report_back": True},
            )(messages)
        return ModelResponse(parts=[TextPart("noted")])

    recorder = Recorder()
    models.models["l"] = scripted(lead_fn)
    parent = await start(container, lead, "write code")
    await drain(container)

    [child] = await threads_of(container, parent.id)
    event_prompt = recorder.prompts[-1]
    assert len(event_prompt) == 1 and f'source="thread:{child.id}"' in event_prompt[0]
    assert "reviewed check v1" in event_prompt[0]
    # 子 thread 的回答不直接推送；父会话（用户发起）处理交回事件的那一轮推送
    deliveries = await tasks_of(container, TaskKind.DELIVER)
    assert child.id not in [d.session_id for d in deliveries]
    assert [d.session_id for d in deliveries].count(parent.id) == 2


async def test_a_thread_that_fails_for_good_reports_its_failure(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("reviewer", model="v")

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="v", body="bad request")

    models.models["v"] = scripted(broken)
    recorder = Recorder()
    calls = {"n": 0}

    def lead_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        recorder(messages, info)
        if calls["n"] == 1:
            return call(
                "delegate_task",
                {"agent": "reviewer", "task": "check", "thread": "review", "report_back": True},
            )(messages)
        return ModelResponse(parts=[TextPart("noted")])

    models.models["l"] = scripted(lead_fn)
    parent = await start(container, lead, "go")
    await drain(container)

    [child] = await threads_of(container, parent.id)
    child_turn = [t for t in await tasks_of(container, TaskKind.TURN) if t.session_id == child.id][-1]
    assert child_turn.status == TaskStatus.FAILED
    assert "failed" in recorder.prompts[-1][0] and "review" in recorder.prompts[-1][0]


async def test_open_reported_threads_get_check_events_without_piling_up(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    parent = await start(container, lead, "go")
    models.models["l"] = scripted(Recorder())
    await drain(container)
    # 子 thread 的一轮排着、还没执行
    target = await container.agents.get(reviewer)
    async with container.db.transaction() as s:
        received = await Threads(container.queue).send(
            s,
            parent_id=parent.id,
            agent=target,
            thread="review",
            prompt="check",
            report_to=ReportTo(session_id=parent.id, reply_to=ReplyTo.USER),
        )
    [child] = await threads_of(container, parent.id)

    assert await container.reports.check() == 1
    assert await container.reports.check() == 0
    async with container.db.transaction() as s:
        [event] = (await s.scalars(select(SessionEvent).where(SessionEvent.session_id == parent.id))).all()
    assert event.source == f"thread:{child.id}" and "queued" in event.content
    assert event.reply_to == ReplyTo.NONE
    assert received.turn.status == TaskStatus.PENDING


async def test_nothing_is_reported_once_the_parent_session_is_gone(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    models.models["l"] = scripted(Recorder())
    models.models["v"] = echo("reviewed")
    parent = await start(container, lead, "go")
    await drain(container)
    target = await container.agents.get(reviewer)
    async with container.db.transaction() as s:
        await Threads(container.queue).send(
            s,
            parent_id=parent.id,
            agent=target,
            thread="review",
            prompt="check",
            report_to=ReportTo(session_id=parent.id, reply_to=ReplyTo.USER),
        )
    # 公开 thread 在父会话删除后作为独立会话保留
    await container.sessions.delete(parent.id)
    await drain(container)
    async with container.db.transaction() as s:
        assert (await s.scalars(select(SessionEvent))).all() == []


async def test_report_back_is_refused_in_a_private_thread(container, make_agent, models):
    """私有 thread 的发问方在同步等回答，没有自己的队列轮次来处理交回的事件。"""
    from pydantic_ai.messages import RetryPromptPart, ToolReturnPart

    from tests.test_threads import answer_with_tool_results

    lead = (await make_agent("lead", model="l")).id
    await make_agent("reviewer", model="v")
    await make_agent("helper", model="h")

    def reviewer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        retries = [p for m in messages for p in m.parts if isinstance(p, RetryPromptPart)]
        if retries:
            return ModelResponse(parts=[TextPart(str(retries[-1].content))])
        return call("delegate_task", {"agent": "helper", "task": "t", "thread": "x", "report_back": True})(
            messages
        )

    models.models["v"] = scripted(reviewer)
    models.models["l"] = steps(
        call("ask_agent", {"agent": "reviewer", "message": "please", "thread": "ask"}),
        answer_with_tool_results("got"),
    )
    parent = await start(container, lead, "go")
    await drain(container)

    history = await container.sessions.history(parent.id)
    returns = [str(p.content) for m in history for p in m.parts if isinstance(p, ToolReturnPart)]
    assert any("not available in a private thread" in r for r in returns)
