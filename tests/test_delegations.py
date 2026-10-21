"""委派的结局（完成、被停止、最终失败、被删除）只交回一次：delegate_task(report_back) 作为事件，ask_agent 作为它
的结果（发问方已经不等时作为事件）；运行中检查；随发起方停止。"""

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.errors import Conflict
from muliao.platform.tables import Task, TaskStatus
from muliao.sessions.kinds import SessionKind
from muliao.sessions.lanes import RUN_TASK
from muliao.sessions.pending import UserAnswered
from muliao.sessions.tables import HoldStatus, Mail, MailKind, MailState, SessionHold
from tests.scripted import scripted
from tests.test_flow import deliver, drain, history_of, start, tasks_of
from tests.test_mailbox import Recorder
from tests.test_threads import call, echo, threads_of


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
    notes = await container.notifications.list(100)
    assert child.id not in [n.session_id for n in notes]
    assert parent.id in [n.session_id for n in notes]


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
    child_turn = [t for t in await tasks_of(container, RUN_TASK) if t.session_id == child.id][-1]
    assert child_turn.status == TaskStatus.FAILED
    reports = [c for p in recorder.prompts for c in p if f'source="thread:{child.id}"' in c]
    assert len(reports) == 1 and "failed" in reports[0] and "review" in reports[0]


async def test_listing_threads_gives_each_its_own_answer_and_failure(container, make_agent, models):
    """list_threads 批量取各 thread 的回答与最近运行：一个答完、一个最终失败，各归各的。"""
    lead = (await make_agent("lead", model="l")).id
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="b", body="bad request")

    models.models["ok"] = echo("answered")
    models.models["b"] = scripted(broken)
    for name, model in (("good", "ok"), ("bad", "b")):
        agent = await make_agent(name, model=model)
        async with container.db.transaction() as s:
            await container.threads.dispatch(
                s,
                owner_id=parent.id,
                agent=agent,
                thread=name,
                prompt="task",
                kind=SessionKind.THREAD,
                call_id=None,
                awaited=False,
                key=name,
            )
    await drain(container)

    async with container.db.transaction() as s:
        views = {v.thread: v.state for v in await container.threads.list_in(s, parent.id)}
    assert views["good"].failure is None and "answered" in views["good"].answer
    assert views["bad"].failure is not None and "bad request" in views["bad"].failure
    assert views["bad"].answer == ""


async def test_open_reported_threads_get_check_events_without_piling_up(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    parent = await start(container, lead, "go")
    models.models["l"] = scripted(Recorder())
    await drain(container)
    # 子 thread 的信件排着、还没交给模型
    target = await container.agents.get(reviewer)
    async with container.db.transaction() as s:
        received = await container.threads.dispatch(
            s,
            owner_id=parent.id,
            agent=target,
            thread="review",
            prompt="check",
            kind=SessionKind.THREAD,
            call_id=None,
            awaited=True,
            key="call-1",
        )
    [child] = await threads_of(container, parent.id)
    assert child.id == received.id
    await deliver(container)

    assert await container.thread_checks.check() == 1
    assert await container.thread_checks.check() == 0
    async with container.db.transaction() as s:
        [event] = (
            await s.scalars(select(Mail).where(Mail.session_id == parent.id, Mail.state == MailState.QUEUED))
        ).all()
        child_mail = (await s.scalars(select(Mail).where(Mail.session_id == child.id))).all()
    assert event.source == f"thread:{child.id}" and "queued" in event.content
    assert not event.awaited
    assert [m.state for m in child_mail] == [MailState.QUEUED]


async def test_nothing_is_reported_once_the_parent_session_is_gone(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    models.models["l"] = scripted(Recorder())
    models.models["v"] = echo("reviewed")
    parent = await start(container, lead, "go")
    await drain(container)
    target = await container.agents.get(reviewer)
    async with container.db.transaction() as s:
        await container.threads.dispatch(
            s,
            owner_id=parent.id,
            agent=target,
            thread="review",
            prompt="check",
            kind=SessionKind.THREAD,
            call_id=None,
            awaited=True,
            key="call-2",
        )
    # 父会话在等这个 thread（占用还在）：先停止它（委派放弃、thread 一起停下），才能删除。公开 thread 在父会话
    # 删除后作为独立会话保留，它之后的结局不再交回任何地方：委派的状态就是发起方的占用，随发起方删除
    await container.stopper(parent.id)
    await container.sessions.delete(parent.id)
    await drain(container)
    async with container.db.transaction() as s:
        assert (await s.scalars(select(Mail).where(Mail.source.is_not(None)))).all() == []
        assert (await s.scalars(select(SessionHold))).all() == []
    assert await _failed_tasks(container) == []


async def test_a_failing_settle_hook_still_records_the_failure_instead_of_leaving_it_running(
    container, make_agent, models, monkeypatch
):
    """收尾钩子自己失败：task 不能卡在 running 占住 lane 直到重启；失败照样记下、可见。"""
    from muliao.sessions.consumer import RunHandler

    agent = (await make_agent("alpha", model="m")).id

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="m", body="bad request")

    async def failing_settle(self, s, task):  # noqa: ANN001, ANN202
        raise RuntimeError("could not report the failure")

    models.models["m"] = scripted(broken)
    monkeypatch.setattr(RunHandler, "failed_in", failing_settle)
    session = await start(container, agent, "go")
    await drain(container)

    [turn] = [t for t in await tasks_of(container, RUN_TASK) if t.session_id == session.id]
    assert turn.status == TaskStatus.FAILED and "bad request" in (turn.error or "")


def test_check_text_follows_the_threads_work_state_and_offers_stop_thread():
    from datetime import UTC, datetime

    from muliao.agents.tables import Agent
    from muliao.platform.tables import Task
    from muliao.sessions.activity import WorkState
    from muliao.sessions.delegation import check_text
    from muliao.sessions.delegation import event_text as _event_text
    from muliao.sessions.events import Ending
    from muliao.sessions.tables import Session

    child = Session(id="c", agent_id="a", title="t", thread="review")
    agent = Agent(id="a", name="reviewer")
    queued = Task(id=1, kind=RUN_TASK, status=TaskStatus.PENDING, attempts=0)
    retrying = Task(id=2, kind=RUN_TASK, status=TaskStatus.PENDING, attempts=2)
    done = Task(id=3, kind=RUN_TASK, status=TaskStatus.DONE, attempts=1)
    running = Task(id=4, kind=RUN_TASK, status=TaskStatus.RUNNING, attempts=1, started_at=datetime.now(UTC))
    now = datetime.now(UTC)
    # 排队中的 thread 也能停：停止清空它的信箱，并交回「被停止」
    assert "stop_thread('review')" in check_text(child, agent, WorkState.RUNNING, queued, now)
    assert "queued and has not started" in check_text(child, agent, WorkState.RUNNING, None, now)
    assert "waiting to run again" in check_text(child, agent, WorkState.RUNNING, retrying, now)
    assert "still working" in check_text(child, agent, WorkState.RUNNING, running, now)
    # 最近一次运行做完了但在等自己开出的工作：照工作状态说在等，不是还排着
    assert "waiting for work it started" in check_text(child, agent, WorkState.WAITING, done, now)
    # 最终失败的运行期间才到的委派信件等定期对账唤醒：thread 此刻空闲，委派还没开始处理（不是错误）
    assert "queued and has not started" in check_text(child, agent, WorkState.IDLE, done, now)
    # 被停止时如实说明，附上停止前说的话
    assert "was stopped" in _event_text("review", agent.name, Ending.STOPPED, "")


async def test_a_failure_to_record_the_failure_itself_does_not_skip_the_settle_hook(container, monkeypatch):
    """记账本身失败（如数据库暂时不可用）不走「跳过收尾」的退路：那条退路只为收尾钩子自己的失败准备。"""
    from muliao.platform.tables import Task
    from muliao.platform.worker import Worker

    calls = {"n": 0}

    async def fake_fail(task_id, error, *, retryable, on_final):  # noqa: ANN001, ANN202
        calls["n"] += 1
        raise RuntimeError("database is locked")

    class Handler:
        async def failed_in(self, s, task):  # noqa: ANN001, ANN202
            """不会被调到。"""

    worker = Worker(container.queue, {RUN_TASK: Handler()}, container.bus, concurrency=1)
    monkeypatch.setattr(container.queue, "fail", fake_fail)

    await worker._record_failure(Task(id=1, kind=RUN_TASK), RuntimeError("boom"), {})  # noqa: SLF001
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
        await container.stopper(parent.id)
        await idle(container)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    [child] = await threads_of(container, parent.id)
    [child_run] = [t for t in await tasks_of(container, RUN_TASK) if t.session_id == child.id]
    assert child_run.status == TaskStatus.DONE
    history = await history_of(container, child.id)
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
    from muliao.agent_runtime.runner import AgentRuntime

    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    original = AgentRuntime.instructions

    async def broken(self, agent, scope, summary, origin_note):  # noqa: ANN001, ANN202
        if agent.id == reviewer:
            raise RuntimeError("cannot build instructions")
        return await original(self, agent, scope, summary, origin_note)

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
    assert await container.thread_checks.check() == 0


async def test_stopping_a_queued_thread_reports_that_it_was_stopped(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)
    target = await container.agents.get(reviewer)
    async with container.db.transaction() as s:
        await container.threads.dispatch(
            s,
            owner_id=parent.id,
            agent=target,
            thread="review",
            prompt="check",
            kind=SessionKind.THREAD,
            call_id=None,
            awaited=True,
            key="call-3",
        )
    [child] = await threads_of(container, parent.id)

    await container.stopper(child.id)
    await deliver(container)

    async with container.db.transaction() as s:
        [event] = (
            await s.scalars(select(Mail).where(Mail.session_id == parent.id, Mail.state == MailState.QUEUED))
        ).all()
        [view] = await container.threads.list_in(s, parent.id)
    assert event.source == f"thread:{child.id}" and "was stopped" in event.content
    # 排着的运行记为完成、没有任何消息：thread 的状态照样能读
    assert (view.state.work, view.state.failure, view.state.answer) == ("idle", None, "")
    assert await container.thread_checks.check() == 0


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
            await container.threads.dispatch(
                s,
                owner_id=parent.id,
                agent=target,
                thread="review",
                prompt=prompt,
                kind=SessionKind.THREAD,
                call_id=None,
                awaited=False,
                key=prompt,
            )

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            await send("second task")  # 第一次运行进行中才到
            raise ModelHTTPError(status_code=400, model_name="v", body="bad request")
        yield "done with it"

    models.models["v"] = FunctionModel(stream_function=stream)
    await send("first task")
    # 最终失败的收尾当场唤醒下一次运行处理之后才到的委派，不等信箱对账
    await drain(container)
    assert await container.mail.reconcile() == 0

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
    assert await container.thread_checks.check() == 0


async def _asking(container, make_agent, models, helper_model):  # noqa: ANN001, ANN202
    """lead 用 ask_agent 问 helper（模型由调用方给），返回父会话与记下 lead 收到的工具结果的列表。"""
    from tests.test_threads import call

    lead = (await make_agent("lead", model="l")).id
    await make_agent("helper", model="h")
    models.models["h"] = helper_model
    seen: list[str] = []
    calls = {"n": 0}

    def lead_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from tests.test_flow import tool_returns

        calls["n"] += 1
        seen.extend(str(r.content) for r in tool_returns(messages))
        if calls["n"] == 1:
            return call("ask_agent", {"agent": "helper", "message": "check", "thread": "h"}, "ask1")(messages)
        return ModelResponse(parts=[TextPart("ok")])

    models.models["l"] = scripted(lead_fn)
    parent = await start(container, lead, "go")
    return parent, seen


async def test_a_user_message_while_waiting_on_an_agent_continues_and_the_answer_arrives_as_an_event(
    container, make_agent, models
):
    import asyncio
    from collections.abc import AsyncIterator

    from pydantic_ai.models.function import FunctionModel

    from tests.test_flow import idle, reply

    gate = asyncio.Event()

    async def slow(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        await gate.wait()
        yield "checked"

    parent, seen = await _asking(container, make_agent, models, FunctionModel(stream_function=slow))

    async def parent_runs_done(n: int) -> bool:
        runs = [r for r in await tasks_of(container, RUN_TASK) if r.session_id == parent.id]
        return len(runs) >= n and all(r.status == TaskStatus.DONE for r in runs)

    worker = asyncio.create_task(container.worker.run_forever())
    try:
        await _until(lambda: parent_runs_done(1))
        # 发问方停在 ask_agent 上：用户插话，它不再等，这次调用记为「回答之后作为事件送达」
        await reply(container, parent.id, "anything else?")
        await _until(lambda: parent_runs_done(2))
        assert any("will reach you as an event" in r for r in seen)
        gate.set()
        await idle(container)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    [child] = await threads_of(container, parent.id)
    async with container.db.transaction() as s:
        events = (
            await s.scalars(
                select(Mail).where(Mail.session_id == parent.id, Mail.source == f"thread:{child.id}")
            )
        ).all()
    # 回答到达时发问方已经不等这次调用了：作为事件交回
    assert [("checked" in e.content, e.kind) for e in events] == [(True, MailKind.EVENT)]


async def test_stopping_a_private_thread_answers_the_waiting_ask_with_stopped(container, make_agent, models):
    import asyncio
    from collections.abc import AsyncIterator

    from pydantic_ai.models.function import FunctionModel

    from tests.test_flow import idle

    running = asyncio.Event()

    async def blocked(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        running.set()
        await asyncio.Event().wait()
        yield "never"

    parent, seen = await _asking(container, make_agent, models, FunctionModel(stream_function=blocked))
    worker = asyncio.create_task(container.worker.run_forever())
    try:
        async with asyncio.timeout(5):
            await running.wait()
        [child] = await threads_of(container, parent.id)
        await container.stopper(child.id)
        await idle(container)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert len(seen) == 1 and "was stopped" in seen[0]


async def test_a_reported_thread_is_deleted_only_after_stopping_and_the_delegator_hears_once(
    container, make_agent, models
):
    lead = (await make_agent("lead", model="l")).id
    reviewer = (await make_agent("reviewer", model="v")).id
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)
    target = await container.agents.get(reviewer)
    async with container.db.transaction() as s:
        received = await container.threads.dispatch(
            s,
            owner_id=parent.id,
            agent=target,
            thread="review",
            prompt="check",
            kind=SessionKind.THREAD,
            call_id=None,
            awaited=False,
            key="call-5",
        )
    await deliver(container)
    # 排着运行的 thread 忙：先停止才能删除；停止时委派结清为「被停止」，删除不再重复交回
    with pytest.raises(Conflict, match="busy"):
        await container.sessions.delete(received.id)
    await container.stopper(received.id)
    await container.sessions.delete(received.id)
    await deliver(container)

    async with container.db.transaction() as s:
        [event] = (
            await s.scalars(select(Mail).where(Mail.session_id == parent.id, Mail.state == MailState.QUEUED))
        ).all()
    assert "was stopped" in event.content
    assert await container.thread_checks.check() == 0


async def test_no_check_ins_while_the_delegator_waits_on_the_ask(container, make_agent, models):
    parent, _seen = await _asking(container, make_agent, models, scripted(Recorder()))
    # 只跑发问方那次运行：它停在 ask_agent 上，子 thread 的运行还排着
    run = await container.queue.claim(container.worker.kinds)
    assert run is not None and run.session_id == parent.id
    await container.queue.complete(run.id, await container.consumer.run(run.id, parent.id))
    # 检查信件要等回答一起才交给模型，这时不发
    assert await container.thread_checks.check() == 0


async def _until(predicate) -> None:  # noqa: ANN001
    import asyncio

    for _ in range(250):
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_an_ask_whose_delegation_never_committed_is_executed_again_on_retry(
    container, make_agent, models, monkeypatch
):
    """回复（含 ask_agent）已保存、投递还没提交时失败：这次调用不算在等回信（没有占用），重试时重新执行它，只委派一次。"""
    from muliao.sessions.threads import Threads as ThreadsClass

    parent, seen = await _asking(container, make_agent, models, echo("checked"))
    original = ThreadsClass.dispatch
    failures = {"n": 0}

    async def flaky(self, s, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if failures["n"] == 0:
            failures["n"] += 1
            raise RuntimeError("database went away")
        return await original(self, s, **kwargs)

    monkeypatch.setattr(ThreadsClass, "dispatch", flaky)
    await drain(container)
    [failed] = [t for t in await tasks_of(container, RUN_TASK) if t.session_id == parent.id]
    assert failed.status == TaskStatus.FAILED

    await container.queue.retry(failed.id)
    await drain(container)

    assert seen == ["checked check"]
    async with container.db.transaction() as s:
        assert (
            len((await s.scalars(select(SessionHold).where(SessionHold.holder.like("delegation:%")))).all())
            == 1
        )


async def test_stopping_the_asker_while_it_waits_abandons_the_ask(container, make_agent, models):
    from muliao.agent_runtime.conventions import STOPPED_TOOL_RESULT

    parent, seen = await _asking(container, make_agent, models, echo("checked"))
    run = await container.queue.claim(container.worker.kinds)
    assert run is not None and run.session_id == parent.id
    await container.queue.complete(run.id, await container.consumer.run(run.id, parent.id))

    await container.stopper(parent.id)
    await drain(container)

    async with container.db.transaction() as s:
        [delegation] = (
            await s.scalars(select(SessionHold).where(SessionHold.holder.like("delegation:%")))
        ).all()
        mail = (await s.scalars(select(Mail).where(Mail.session_id == parent.id))).all()
    assert delegation.status == HoldStatus.CLEARED
    # 发问方的这次调用补上了「被停止」，之后没有回答投回来，也没有再运行
    history = await history_of(container, parent.id)
    from pydantic_ai.messages import ToolReturnPart

    returns = [p for m in history for p in m.parts if isinstance(p, ToolReturnPart)]
    assert [r.content for r in returns] == [STOPPED_TOOL_RESULT]
    assert [m.kind for m in mail] == [MailKind.MESSAGE] and seen == []


async def test_a_question_and_an_ask_in_one_reply_wait_for_both(client, container, make_agent, models):
    from pydantic_ai.messages import ToolCallPart

    from tests.conftest import login
    from tests.test_flow import tool_returns
    from tests.test_questions import QUESTIONS

    await login(client)
    lead = await make_agent("lead", model="l")
    await make_agent("helper", model="h")
    models.models["h"] = echo("checked")
    results: list[str] = []

    def lead_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if returns := tool_returns(messages):
            results.extend(r.tool_name for r in returns)
            return ModelResponse(parts=[TextPart("ok")])
        return ModelResponse(
            parts=[
                ToolCallPart("ask_user", {"questions": QUESTIONS[:1]}, tool_call_id="q"),
                ToolCallPart(
                    "ask_agent", {"agent": "helper", "message": "x", "thread": "h"}, tool_call_id="a"
                ),
            ]
        )

    models.models["l"] = scripted(lead_fn)
    session_id = (await client.post("/api/sessions", json={"agent_id": lead.id, "prompt": "go"})).json()["id"]
    await drain(container)
    # helper 已经答了，用户还没答：还在等
    assert results == []

    await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"prompt": "", "answers": {"q": [{"selected": ["日线"]}]}},
    )
    await drain(container)
    assert sorted(results) == ["ask_agent", "ask_user"]


async def test_an_answer_arriving_mid_run_reaches_the_model_as_late_text(
    client, container, make_agent, models
):
    """提问已被跳过（用户发了消息）、运行进行中才到的回答：作为迟到的回答并进下一次请求，不丢。"""
    from collections.abc import AsyncIterator

    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from muliao.sessions.mailbox import post_in
    from muliao.sessions.questions import AnsweredQuestion, AskResult
    from tests.conftest import login
    from tests.test_questions import QUESTIONS

    await login(client)
    agent = await make_agent("analyst", model="m")
    requests: list[ModelRequest] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator:
        responses = sum(isinstance(m, ModelResponse) for m in messages)
        requests.append(messages[-1])
        if responses == 0:
            yield {
                0: DeltaToolCall(
                    name="ask_user", json_args=json.dumps({"questions": QUESTIONS[:1]}), tool_call_id="q"
                )
            }
        elif responses == 1:
            # 用户跳过了提问；这次运行进行中，那次提问的回答才到
            answer = AskResult(
                answered=True,
                answers=[AnsweredQuestion(question="看哪个周期？", selected=["日线"], other=None)],
                note=None,
            )
            async with container.db.transaction() as s:
                await post_in(
                    s,
                    container.queue,
                    session_id,
                    kind=MailKind.ANSWER,
                    content="",
                    answers={"q": UserAnswered.of(answer).model_dump()},
                    awaited=True,
                )
            yield {0: DeltaToolCall(name="memory_append", json_args='{"entry": "- x"}', tool_call_id="m")}
        else:
            yield "ok"

    import json

    models.models["m"] = FunctionModel(stream_function=stream)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "go"})).json()[
        "id"
    ]
    await drain(container)
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "skip it"})
    await drain(container)

    prompts = [str(p.content) for p in requests[-1].parts if isinstance(p, UserPromptPart)]
    assert any("already been closed" in p and "日线" in p for p in prompts)


async def test_a_private_thread_answers_only_after_its_background_job_finishes(container, make_agent, models):
    """私有 thread 开了后台任务就先回了一句：它的工作还没做完，委派不结清；任务结束后的那次运行给出的才是结局。"""
    import asyncio

    from pydantic_ai.messages import ToolCallPart

    from tests.test_flow import last_user_prompt, tool_returns

    def helper_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "job:" in last_user_prompt(messages):
            return ModelResponse(parts=[TextPart("job finished: all good")])
        if tool_returns(messages):
            return ModelResponse(parts=[TextPart("started, will report")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "run_shell", {"command": "sleep 0.2; echo hi", "background": True}, tool_call_id="bg"
                )
            ]
        )

    parent, seen = await _asking(container, make_agent, models, scripted(helper_fn))
    worker = asyncio.create_task(container.worker.run_forever())
    try:
        await _until(lambda: _has(seen))
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert seen == ["job finished: all good"]


async def test_a_thread_waiting_on_its_own_background_job_is_listed_as_waiting_not_done(
    container, make_agent, models
):
    """thread 开了后台任务、先回了一句就结束了这次运行：它的工作还没做完，list_threads 报 waiting（附上那句中间的
    回答），不报做完；任务结束、给出结局后才是 idle。"""
    import asyncio

    from pydantic_ai.messages import ToolCallPart

    from muliao.sessions.activity import WorkState
    from muliao.sessions.mailbox import latest_run
    from tests.test_flow import last_user_prompt, tool_returns

    def helper_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "job:" in last_user_prompt(messages):
            return ModelResponse(parts=[TextPart("job finished: all good")])
        if tool_returns(messages):
            return ModelResponse(parts=[TextPart("started, will report")])
        return ModelResponse(
            parts=[ToolCallPart("run_shell", {"command": "sleep 2", "background": True}, tool_call_id="bg")]
        )

    parent, seen = await _asking(container, make_agent, models, scripted(helper_fn))

    async def states():  # noqa: ANN202
        async with container.db.transaction() as s:
            return [v.state for v in await container.threads.list_in(s, parent.id)]

    async def waiting_with_its_first_answer() -> bool:
        # 回答先于运行的收尾提交：等收尾之后再看
        return [(x.work, x.answer) for x in await states()] == [(WorkState.WAITING, "started, will report")]

    worker = asyncio.create_task(container.worker.run_forever())
    try:
        await _until(waiting_with_its_first_answer)
        [state] = await states()
        assert state.failure is None
        # 它最近一次运行已经做完（按最近一次运行判断会把它当成做完、把中间的回答当结局）
        [child] = await threads_of(container, parent.id)
        async with container.db.transaction() as s:
            assert (await latest_run(s, child.id)).status == TaskStatus.DONE
        await _until(lambda: _has(seen))
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    [state] = await states()
    assert (state.work, state.answer) == (WorkState.IDLE, "job finished: all good")


async def _has(items: list) -> bool:  # noqa: ANN001
    return bool(items)


async def test_an_outcome_arriving_during_the_threads_last_request_is_waited_for(
    container, make_agent, models
):
    """thread 最后一次请求期间，它的后台工作的结局才进信箱：这次的回答没看到它，委派不结清，等处理它的那次运行。"""
    from collections.abc import AsyncIterator

    from pydantic_ai.models.function import FunctionModel

    from muliao.sessions.mailbox import post_in
    from tests.test_flow import last_user_prompt

    calls = {"n": 0}

    async def helper(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            [child] = await threads_of(container, parent.id)
            async with container.db.transaction() as s:
                await post_in(
                    s,
                    container.queue,
                    child.id,
                    kind=MailKind.EVENT,
                    source="job:1",
                    content="exit 0",
                    awaited=False,
                )
            yield "started, will report"
        else:
            yield f"final: {last_user_prompt(messages)[:20]}"

    parent, seen = await _asking(container, make_agent, models, FunctionModel(stream_function=helper))
    await drain(container)
    assert len(seen) == 1 and seen[0].startswith("final:")


async def _failed_tasks(container) -> list[Task]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Task).where(Task.status == TaskStatus.FAILED))).all())


async def test_stopping_the_asker_stops_its_threads_level_by_level(container, make_agent, models):
    """发起方被停止：停止事务清掉它的委派占用，并给做这些工作的 thread 发 StopThread；thread 的停止再停下它自己委派
    出去的 thread。结局一律不再交回。"""
    from muliao.sessions.tables import MailState

    lead = (await make_agent("lead", model="l")).id
    middle = await container.agents.get((await make_agent("middle", model="m")).id)
    leaf = await container.agents.get((await make_agent("leaf", model="f")).id)
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)

    async def dispatch(owner_id: str, agent, thread: str):  # noqa: ANN001, ANN202
        async with container.db.transaction() as s:
            return await container.threads.dispatch(
                s,
                owner_id=owner_id,
                agent=agent,
                thread=thread,
                prompt="work",
                kind=SessionKind.THREAD,
                call_id=None,
                awaited=False,
                key=f"k-{thread}",
            )

    first = await dispatch(parent.id, middle, "first")
    second = await dispatch(first.id, leaf, "second")
    # 两个 thread 的运行都还排着
    await container.stopper(parent.id)
    await drain(container)

    async with container.db.transaction() as s:
        holds = (await s.scalars(select(SessionHold))).all()
        queued = (
            await s.scalars(
                select(Mail).where(Mail.session_id.in_([first.id, second.id]), Mail.state == MailState.QUEUED)
            )
        ).all()
        thread_events = (await s.scalars(select(Mail).where(Mail.source.like("thread:%")))).all()
    assert {h.status for h in holds} == {HoldStatus.CLEARED}
    assert queued == [] and thread_events == []
    runs = [t for t in await tasks_of(container, RUN_TASK) if t.session_id in (first.id, second.id)]
    assert runs and all(t.status == TaskStatus.DONE for t in runs)
    assert await _failed_tasks(container) == []
