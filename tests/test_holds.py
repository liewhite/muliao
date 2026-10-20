"""占用与结清（sessions.holds、sessions.settlement，docs/domain-design.md 4.1、4.2）：会话只看自己的数据判断工作是否
做完、什么时候结清；委派的结局只交回一次；停止之后迟到的结局不投。"""

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.delegations.tables import Delegation, DelegationStatus
from muliao.errors import InvalidInput
from muliao.platform.tables import Task, TaskKind
from muliao.sessions.holds import hold_in
from muliao.sessions.settlement import Delivered, deliver_in
from muliao.sessions.tables import HoldStatus, Mail, SessionHold
from tests.scripted import scripted
from tests.test_delegations import _asking
from tests.test_flow import drain, start, tool_returns
from tests.test_mailbox import Recorder
from tests.test_threads import call, echo, threads_of


async def _holds(container, session_id: str) -> list[SessionHold]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(SessionHold).where(SessionHold.session_id == session_id))).all())


async def test_an_answered_ask_is_not_executed_again_and_its_hold_stays_as_the_record(
    container, make_agent, models, monkeypatch
):
    from muliao.sessions.conversation import Threads

    dispatched: list[str] = []
    original = Threads.dispatch

    async def counting(self, s, **kwargs):  # noqa: ANN001, ANN003, ANN202
        dispatched.append(kwargs["key"])
        return await original(self, s, **kwargs)

    monkeypatch.setattr(Threads, "dispatch", counting)
    parent, seen = await _asking(container, make_agent, models, echo("checked"))
    await drain(container)

    # 发问方续跑时拿到回答作为这次调用的结果，没有再执行 ask_agent
    assert seen == ["checked check"] and dispatched == ["ask1"]
    # 占用已释放但保留：它是「这次调用已经交出去」的永久记录
    [hold] = await _holds(container, parent.id)
    assert (hold.call_id, hold.status) == ("ask1", HoldStatus.RELEASED)
    async with container.db.transaction() as s:
        [delegation] = (await s.scalars(select(Delegation))).all()
    assert delegation.status == DelegationStatus.SETTLED


async def test_a_dispatch_to_a_mismatched_thread_writes_nothing(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    reviewer = await container.agents.get((await make_agent("reviewer", model="v")).id)
    other = await container.agents.get((await make_agent("other", model="o")).id)
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        await container.threads.send(s, parent_id=parent.id, agent=reviewer, thread="review", prompt="x")
    before = await _mail_count(container)
    events_before = await _event_count(container)

    with pytest.raises(InvalidInput, match="another agent"):
        async with container.db.transaction() as s:
            await container.threads.dispatch(
                s,
                owner_id=parent.id,
                agent=other,
                thread="review",
                prompt="y",
                private=False,
                call_id=None,
                awaited=False,
                key="k",
            )

    assert await _holds(container, parent.id) == []
    assert await _mail_count(container) == before
    assert await _event_count(container) == events_before


async def _event_count(container) -> int:
    async with container.db.transaction() as s:
        return len((await s.scalars(select(Task).where(Task.kind == TaskKind.EVENT))).all())


async def _mail_count(container) -> int:
    async with container.db.transaction() as s:
        return len((await s.scalars(select(Mail))).all())


async def test_an_outcome_after_the_session_was_stopped_is_dropped_without_waking_it(container, make_agent):
    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    async with container.db.transaction() as s:
        await hold_in(s, session.id, "job:1")
    await container.consumer.stop_session(session.id)
    before = await _mail_count(container)

    async with container.db.transaction() as s:
        delivered = await deliver_in(
            s, container.queue, session.id, "job:1", key="k", awaited=True, source="job:1", event_text="done"
        )

    assert delivered == Delivered.DROPPED
    assert await _mail_count(container) == before
    [hold] = await _holds(container, session.id)
    assert hold.status == HoldStatus.CLEARED


async def test_an_outcome_for_a_hold_that_was_never_registered_is_a_bug(container, make_agent):
    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    with pytest.raises(RuntimeError, match="has no hold"):
        async with container.db.transaction() as s:
            await deliver_in(
                s, container.queue, session.id, "job:9", key="k", awaited=True, source="job:9", event_text="x"
            )


async def test_a_final_failure_settles_delegations_handled_by_an_earlier_run(container, make_agent, models):
    """thread 的第一次运行处理了委派、停在它自己的 ask_agent 上；拿到回答后续跑的那次运行最终失败。委派按会话范围
    结清为失败，发起方不会一直等。"""
    lead = (await make_agent("lead", model="l")).id
    await make_agent("middle", model="m")
    await make_agent("leaf", model="f")
    models.models["f"] = echo("leaf says")
    lead_seen: list[str] = []

    def lead_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        lead_seen.extend(str(r.content) for r in tool_returns(messages))
        if len(lead_seen) == 0 and sum(isinstance(m, ModelResponse) for m in messages) == 0:
            return call("ask_agent", {"agent": "middle", "message": "go deeper", "thread": "mid"}, "a1")(
                messages
            )
        return ModelResponse(parts=[TextPart("ok")])

    def middle_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if sum(isinstance(m, ModelResponse) for m in messages) == 0:
            return call("ask_agent", {"agent": "leaf", "message": "q", "thread": "leaf"}, "a2")(messages)
        raise ModelHTTPError(status_code=400, model_name="m", body="bad request")

    models.models["l"] = scripted(lead_fn)
    models.models["m"] = scripted(middle_fn)
    parent = await start(container, lead, "go")
    await drain(container)

    [middle] = await threads_of(container, parent.id)
    async with container.db.transaction() as s:
        runs = (
            await s.scalars(select(Task).where(Task.kind == TaskKind.RUN, Task.session_id == middle.id))
        ).all()
    assert [t.status for t in runs][-1] == "failed"
    # 发起方拿到失败作为 ask_agent 的结果，继续往下走
    assert len(lead_seen) == 1 and "failed" in lead_seen[0] and "bad request" in lead_seen[0]


async def test_a_thread_that_settles_before_the_dispatch_is_delivered_still_reports_back(
    container, make_agent, models
):
    """子会话的运行在 DelegationDispatched 投递之前就结清了：两条投递在同一条 lane 上按发布顺序处理，委派行先建立，
    结局照常交回。"""
    from muliao.platform.worker import Worker

    lead = (await make_agent("lead", model="l")).id
    reviewer = await container.agents.get((await make_agent("reviewer", model="v")).id)
    recorder = Recorder()
    models.models["l"] = scripted(recorder)
    models.models["v"] = echo("reviewed")
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        child = await container.threads.dispatch(
            s,
            owner_id=parent.id,
            agent=reviewer,
            thread="review",
            prompt="check",
            private=False,
            call_id=None,
            awaited=False,
            key="k",
        )
    # 只跑运行（不投递事件）：thread 做完并结清
    runs_only = Worker(
        container.queue,
        {TaskKind.RUN: container.worker._handlers[TaskKind.RUN]},
        container.bus,
        concurrency=1,
        task_timeout_seconds=5,
    )  # noqa: SLF001
    import anyio

    async with anyio.create_task_group() as group:
        group.start_soon(runs_only.run_forever)
        while True:
            await anyio.sleep(0.02)
            async with container.db.transaction() as s:
                if (
                    await s.get(
                        Mail, (await s.scalars(select(Mail.id).where(Mail.session_id == child.id))).first()
                    )
                ).correlation_settled:
                    break
        group.cancel_scope.cancel()

    await drain(container)
    assert any(f"thread:{child.id}" in p and "reviewed check" in p for p in recorder.prompts[-1])
    async with container.db.transaction() as s:
        [delegation] = (await s.scalars(select(Delegation))).all()
    assert delegation.status == DelegationStatus.SETTLED


async def test_a_hold_registered_while_the_run_winds_down_is_cleared_by_the_stop(
    container, make_agent, models
):
    """停止事务在运行退出之后执行：运行退出途中才登记的占用也被清掉，会话不会因此一直「忙」。"""
    import asyncio

    from muliao.sessions import consumer as consumer_module

    agent = (await make_agent("alpha", model="m")).id
    started = asyncio.Event()

    async def slow(messages: list[ModelMessage], info: AgentInfo):  # noqa: ANN202
        started.set()
        await asyncio.sleep(30)
        yield "never"

    from pydantic_ai.models.function import FunctionModel

    models.models["m"] = FunctionModel(stream_function=slow)
    session = await start(container, agent, "go")
    original = consumer_module.Consumer._stopped

    async def hold_then_stop(self, run_id, session_id):  # noqa: ANN001, ANN202
        # 运行刚退出、停止事务之前：模拟退出途中提交的占用（如后台任务的启动）
        async with container.db.transaction() as s:
            await hold_in(s, session_id, "job:77")
        return await original(self, run_id, session_id)

    consumer_module.Consumer._stopped = hold_then_stop
    try:
        worker = asyncio.create_task(container.worker.run_forever())
        await asyncio.wait_for(started.wait(), 5)
        await container.consumer.stop_session(session.id)
        from tests.test_flow import idle

        await idle(container)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    finally:
        consumer_module.Consumer._stopped = original

    [hold] = await _holds(container, session.id)
    assert hold.status == HoldStatus.CLEARED
    await container.sessions.delete(session.id)


async def test_a_run_stopped_right_after_it_was_claimed_does_not_execute(container, make_agent, models):
    called: list[bool] = []
    agent = (await make_agent("alpha", model="m")).id
    models.models["m"] = scripted(lambda m, i: called.append(True) or ModelResponse(parts=[TextPart("x")]))
    session = await start(container, agent, "go")
    run = await container.queue.claim([TaskKind.RUN])
    assert run is not None

    # 领取之后、登记可停止的运行之前停止：停止直接执行停止事务
    await container.consumer.stop_session(session.id)
    commit = await container.consumer.run(run.id, session.id)
    await container.queue.complete(run.id, commit)

    assert called == []


async def test_reconcile_settles_delegations_whose_failure_hook_failed(
    container, make_agent, models, monkeypatch
):
    from muliao.sessions.consumer import RunHandler

    lead = (await make_agent("lead", model="l")).id
    reviewer = await container.agents.get((await make_agent("reviewer", model="v")).id)
    recorder = Recorder()
    models.models["l"] = scripted(recorder)

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="v", body="bad request")

    models.models["v"] = scripted(broken)
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        await container.threads.dispatch(
            s,
            owner_id=parent.id,
            agent=reviewer,
            thread="review",
            prompt="check",
            private=False,
            call_id=None,
            awaited=False,
            key="k",
        )

    async def failing_settle(self, s, task):  # noqa: ANN001, ANN202
        raise RuntimeError("could not settle")

    monkeypatch.setattr(RunHandler, "failed_in", failing_settle)
    await drain(container)
    monkeypatch.undo()
    async with container.db.transaction() as s:
        assert (await s.scalars(select(Delegation))).one().status == DelegationStatus.RUNNING

    await container.mail.reconcile()
    await drain(container)

    async with container.db.transaction() as s:
        assert (await s.scalars(select(Delegation))).one().status == DelegationStatus.SETTLED
    assert any("failed" in p and "bad request" in p for p in recorder.prompts[-1])


async def test_stopping_a_thread_the_user_already_deleted_is_not_an_error(container, make_agent, models):
    from muliao.sessions.halt import Halted

    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    await drain(container)
    await container.sessions.delete(session.id)
    assert await container.consumer.stop_session(session.id) == Halted.GONE


async def test_an_outcome_is_delivered_even_if_the_threads_agent_was_deleted_meanwhile(
    container, make_agent, models
):
    """结清事件发布之后、投递之前，thread 的 agent 被删除：交回的文字用结清时的 agent 名，不读 agent 表，投递照常完成。"""
    lead = (await make_agent("lead", model="l")).id
    reviewer_id = (await make_agent("reviewer", model="v")).id
    reviewer = await container.agents.get(reviewer_id)
    recorder = Recorder()
    models.models["l"] = scripted(recorder)
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        await container.threads.dispatch(
            s,
            owner_id=parent.id,
            agent=reviewer,
            thread="review",
            prompt="check",
            private=False,
            call_id=None,
            awaited=False,
            key="k",
        )
    [child] = await threads_of(container, parent.id)
    # thread 被停止：结清事件发布了，还没投递
    await container.consumer.stop_session(child.id)
    await container.agent_lifecycle.delete(reviewer_id, author="user")

    await drain(container)

    async with container.db.transaction() as s:
        assert (await s.scalars(select(Delegation))).one().status == DelegationStatus.SETTLED
    assert any("(reviewer) was stopped" in p for p in recorder.prompts[-1])
