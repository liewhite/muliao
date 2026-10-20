"""delegate_task(report_back)：thread 的结果（完成、被停止、最终失败）作为事件交回发起委派的会话。"""

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.models import Mail, MailState, TaskKind, TaskStatus
from muliao.runtime.conversation import ReportTo, Threads
from tests.scripted import scripted
from tests.test_flow import drain, start, tasks_of
from tests.test_mailbox import Recorder
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
    [event_prompt] = [p for p in recorder.prompts if any(f'source="thread:{child.id}"' in c for c in p)]
    assert len(event_prompt) == 1 and "reviewed check v1" in event_prompt[0]
    # 子 thread 的回答不直接推送；父会话（用户发起）处理交回事件的运行推送
    deliveries = await tasks_of(container, TaskKind.DELIVER)
    assert child.id not in [d.session_id for d in deliveries]
    assert parent.id in [d.session_id for d in deliveries]


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
    reports = [c for p in recorder.prompts for c in p if f'source="thread:{child.id}"' in c]
    assert len(reports) == 1 and "failed" in reports[0] and "review" in reports[0]


async def test_open_reported_threads_get_check_events_without_piling_up(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    parent = await start(container, lead, "go")
    models.models["l"] = scripted(Recorder())
    await drain(container)
    # 子 thread 的信件排着、还没交给模型
    target = await container.agents.get(reviewer)
    async with container.db.transaction() as s:
        received = await Threads(container.queue).send(
            s,
            parent_id=parent.id,
            agent=target,
            thread="review",
            prompt="check",
            report_to=ReportTo(session_id=parent.id, awaited=True),
        )
    [child] = await threads_of(container, parent.id)

    assert await container.reports.check() == 1
    assert await container.reports.check() == 0
    async with container.db.transaction() as s:
        [event] = (
            await s.scalars(select(Mail).where(Mail.session_id == parent.id, Mail.state == MailState.QUEUED))
        ).all()
    assert event.source == f"thread:{child.id}" and "queued" in event.content
    assert not event.awaited
    assert [m.state for m in received.mail] == [MailState.QUEUED]


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
            report_to=ReportTo(session_id=parent.id, awaited=True),
        )
    # 公开 thread 在父会话删除后作为独立会话保留
    await container.sessions.delete(parent.id)
    await drain(container)
    async with container.db.transaction() as s:
        assert (await s.scalars(select(Mail).where(Mail.source.is_not(None)))).all() == []


async def test_report_back_is_refused_in_a_private_thread(container, make_agent, models):
    """私有 thread 的发问方在同步等回答，交回的事件没有信箱可投（私有 thread 只收发问方的问题）。"""
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


async def test_a_failing_settle_hook_still_records_the_failure_instead_of_leaving_it_running(
    container, make_agent, models, monkeypatch
):
    """收尾钩子自己失败：task 不能卡在 running 占住 lane 直到重启；失败照样记下、可见。"""
    from muliao.runtime.consumer import RunHandler

    agent = (await make_agent("alpha", model="m")).id

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="m", body="bad request")

    async def failing_settle(self, s, task):  # noqa: ANN001, ANN202
        raise RuntimeError("could not report the failure")

    models.models["m"] = scripted(broken)
    monkeypatch.setattr(RunHandler, "failed_in", failing_settle)
    session = await start(container, agent, "go")
    await drain(container)

    [turn] = [t for t in await tasks_of(container, TaskKind.TURN) if t.session_id == session.id]
    assert turn.status == TaskStatus.FAILED and "bad request" in (turn.error or "")


def test_check_text_offers_stop_thread_whether_the_thread_is_queued_or_running():
    from datetime import UTC, datetime

    from muliao.models import Agent, Session, Task
    from muliao.runtime.reports import _check_text, finished_text

    child = Session(id="c", agent_id="a", title="t", thread="review")
    agent = Agent(id="a", name="reviewer")
    queued = Task(id=1, kind=TaskKind.TURN, status=TaskStatus.PENDING, attempts=0)
    retrying = Task(id=2, kind=TaskKind.TURN, status=TaskStatus.PENDING, attempts=2)
    now = datetime.now(UTC)
    # 排队中的 thread 也能停：停止清空它的信箱，并交回「被停止」
    assert "stop_thread('review')" in _check_text(child, agent, queued, now)
    assert "waiting to run again" in _check_text(child, agent, retrying, now)
    # 被停止时如实说明，附上停止前说的话
    assert "was stopped" in finished_text(child, agent, "", stopped=True)


async def test_a_failure_to_record_the_failure_itself_does_not_skip_the_settle_hook(container, monkeypatch):
    """记账本身失败（如数据库暂时不可用）不走「跳过收尾」的退路：那条退路只为收尾钩子自己的失败准备。"""
    from muliao.models import Task
    from muliao.runtime.worker import Worker

    calls = {"n": 0}

    async def fake_fail(task_id, error, *, retryable, on_final):  # noqa: ANN001, ANN202
        calls["n"] += 1
        raise RuntimeError("database is locked")

    class Handler:
        async def failed_in(self, s, task):  # noqa: ANN001, ANN202
            """不会被调到。"""

    worker = Worker(
        container.queue, {TaskKind.TURN: Handler()}, container.bus, concurrency=1, task_timeout_seconds=5
    )
    monkeypatch.setattr(container.queue, "fail", fake_fail)

    await worker._record_failure(Task(id=1, kind=TaskKind.TURN), RuntimeError("boom"), {})  # noqa: SLF001
    # 只尝试一次：没有因为记账失败就去掉钩子重来
    assert calls["n"] == 1


async def test_stopping_the_parent_stops_its_reported_threads_and_they_do_not_report(
    container, make_agent, models
):
    import asyncio
    from collections.abc import AsyncIterator

    from pydantic_ai.models.function import FunctionModel

    from tests.test_flow import idle

    lead = (await make_agent("lead", model="l")).id
    await make_agent("reviewer", model="v")
    running = asyncio.Event()

    async def blocked(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        running.set()
        await asyncio.Event().wait()
        yield "never"

    models.models["v"] = FunctionModel(stream_function=blocked)
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
    worker = asyncio.create_task(container.worker.run_forever())
    try:
        async with asyncio.timeout(5):
            await running.wait()
        await container.consumer.stop_session(parent.id)
        await idle(container)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    [child] = await threads_of(container, parent.id)
    [child_run] = [t for t in await tasks_of(container, TaskKind.TURN) if t.session_id == child.id]
    assert child_run.status == TaskStatus.DONE
    history = await container.sessions.history(child.id)
    assert history[-1].metadata and history[-1].metadata.get("muliao_stopped")
    # 被一起停下的 thread 不再给父会话投信：父会话没有因为它再运行
    assert not any(f'source="thread:{child.id}"' in c for p in recorder.prompts for c in p)
    async with container.db.transaction() as s:
        assert (
            await s.scalars(select(Mail).where(Mail.session_id == parent.id, Mail.source.is_not(None)))
        ).all() == []


async def test_a_thread_failing_before_its_first_step_reports_the_failure_once(
    container, make_agent, models, monkeypatch
):
    """运行在第一次保存之前就最终失败（如组装指令出错）：委派信件一直排着、不会再被唤醒，也要交回失败，之后不再检查。"""
    from muliao.runtime.runner import AgentRuntime

    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    original = AgentRuntime.instructions

    async def broken(self, agent, scope, summary):  # noqa: ANN001, ANN202
        if agent.id == reviewer:
            raise RuntimeError("cannot build instructions")
        return await original(self, agent, scope, summary)

    monkeypatch.setattr(AgentRuntime, "instructions", broken)
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
    reports = [c for p in recorder.prompts for c in p if f'source="thread:{child.id}"' in c]
    assert len(reports) == 1 and "cannot build instructions" in reports[0]
    # 已经交回：不再给父会话发检查
    assert await container.reports.check() == 0


async def test_stopping_a_queued_thread_reports_that_it_was_stopped(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    models.models["l"] = scripted(Recorder())
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
            report_to=ReportTo(session_id=parent.id, awaited=True),
        )
    [child] = await threads_of(container, parent.id)

    await container.consumer.stop_session(child.id)

    async with container.db.transaction() as s:
        [event] = (
            await s.scalars(select(Mail).where(Mail.session_id == parent.id, Mail.state == MailState.QUEUED))
        ).all()
        state = await Threads(container.queue).state(s, child)
    assert event.source == f"thread:{child.id}" and "was stopped" in event.content
    # 排着的运行记为完成、没有任何消息：thread 的状态照样能读
    assert (state.status, state.text) == ("done", "")
    assert await container.reports.check() == 0


async def test_a_delegation_arriving_during_a_failing_run_is_still_handled_and_reported(
    container, make_agent, models
):
    """thread 的运行最终失败：交给过它的委派交回失败；它运行期间才到的委派不算失败，之后照常处理、交回真实结果。"""
    from collections.abc import AsyncIterator

    from pydantic_ai.models.function import FunctionModel

    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)
    target = await container.agents.get(reviewer)
    calls = {"n": 0}

    async def send(prompt: str) -> None:
        async with container.db.transaction() as s:
            await Threads(container.queue).send(
                s,
                parent_id=parent.id,
                agent=target,
                thread="review",
                prompt=prompt,
                report_to=ReportTo(session_id=parent.id, awaited=False),
            )

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            await send("second task")  # 第一次运行进行中才到
            raise ModelHTTPError(status_code=400, model_name="v", body="bad request")
        yield "done with it"

    models.models["v"] = FunctionModel(stream_function=stream)
    await send("first task")
    await drain(container)
    # 失败的运行不唤醒下一次；之后才到的委派由对账唤醒
    assert await container.mail.reconcile() >= 1
    await drain(container)

    [child] = await threads_of(container, parent.id)
    async with container.db.transaction() as s:
        events = (
            await s.scalars(
                select(Mail).where(Mail.session_id == parent.id, Mail.source == f"thread:{child.id}")
            )
        ).all()
    texts = [e.content for e in events]
    assert len(texts) == 2
    assert "failed" in texts[0] and "finished your task" in texts[1] and "done with it" in texts[1]
    assert await container.reports.check() == 0
