"""占用与结清（sessions.holds、sessions.settlement，docs/architecture.md 第 7、8 节）：会话只看自己的数据判断工作是否
做完、什么时候结清；委派的结局只交回一次；停止之后迟到的结局不投。"""

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.platform.tables import Task, TaskStatus
from muliao.sessions.holds import hold_in
from muliao.sessions.lanes import RUN_TASK
from muliao.sessions.tables import HoldStatus, Mail, SessionHold
from muliao.sessions.work import Delivered, deliver_in, hold_of_in
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
    from muliao.sessions.delegation import Delegations

    dispatched: list[str] = []
    original = Delegations.dispatch

    async def counting(self, s, **kwargs):  # noqa: ANN001, ANN003, ANN202
        dispatched.append(kwargs["call_id"])
        return await original(self, s, **kwargs)

    monkeypatch.setattr(Delegations, "dispatch", counting)
    parent, seen = await _asking(container, make_agent, models, echo("checked"))
    await drain(container)

    # 发问方续跑时拿到回答作为这次调用的结果，没有再执行 ask_agent
    assert seen == ["checked check"] and dispatched == ["ask1"]
    # 占用已释放但保留：它是「这次调用已经交出去」的永久记录
    [hold] = await _holds(container, parent.id)
    assert (hold.call_id, hold.status) == ("ask1", HoldStatus.RELEASED)
    async with container.db.transaction() as s:
        [delegation] = (
            await s.scalars(select(SessionHold).where(SessionHold.thread_call_id.is_not(None)))
        ).all()
    assert delegation.status == HoldStatus.RELEASED


async def _mail_count(container) -> int:
    async with container.db.transaction() as s:
        return len((await s.scalars(select(Mail))).all())


async def test_an_outcome_after_the_session_was_stopped_is_dropped_without_waking_it(container, make_agent):
    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    async with container.db.transaction() as s:
        await hold_in(s, session.id, "job:1", awaited=False)
    await container.stopper(session.id)
    before = await _mail_count(container)

    async with container.db.transaction() as s:
        delivered = await deliver_in(
            s,
            container.queue,
            await hold_of_in(s, session.id, "job:1"),
            source="job:1",
            event_text="done",
        )

    assert delivered == Delivered.DROPPED
    assert await _mail_count(container) == before
    [hold] = await _holds(container, session.id)
    assert hold.status == HoldStatus.CLEARED


async def test_handing_back_a_released_hold_again_is_a_bug(container, make_agent):
    """交回与结局的记录同一个事务，每项占用只交回一次：再交回一次当场报错，不靠去重悄悄吞掉。"""
    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    async with container.db.transaction() as s:
        await hold_in(s, session.id, "job:1", awaited=False)

    async def hand_back() -> Delivered:
        async with container.db.transaction() as s:
            return await deliver_in(
                s,
                container.queue,
                await hold_of_in(s, session.id, "job:1"),
                source="job:1",
                event_text="done",
            )

    assert await hand_back() == Delivered.EVENT
    before = await _mail_count(container)
    with pytest.raises(RuntimeError, match="already released"):
        await hand_back()
    assert await _mail_count(container) == before


async def test_an_outcome_for_a_hold_that_was_never_registered_is_a_bug(container, make_agent):
    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    with pytest.raises(RuntimeError, match="has no hold"):
        async with container.db.transaction() as s:
            await hold_of_in(s, session.id, "job:9")


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
            return call(
                "ask_agent",
                {"agent": "middle", "message": "go deeper", "thread": "mid"},
                "a1",
            )(messages)
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
            await s.scalars(select(Task).where(Task.kind == RUN_TASK, Task.session_id == middle.id))
        ).all()
    assert [t.status for t in runs][-1] == "failed"
    # 发起方拿到失败作为 ask_agent 的结果，继续往下走
    assert len(lead_seen) == 1 and "failed" in lead_seen[0] and "bad request" in lead_seen[0]


async def test_a_thread_hands_its_outcome_back_in_the_transaction_that_settles_it(
    container, make_agent, models
):
    """委派的交回不经事件投递：thread 结清关联的那个事务就把结局投进发起方的信箱、释放占用。"""
    from tests.test_flow import run_only

    lead = (await make_agent("lead", model="l")).id
    reviewer = await container.agents.get((await make_agent("reviewer", model="v")).id)
    recorder = Recorder()
    models.models["l"] = scripted(recorder)
    models.models["v"] = echo("reviewed")
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        child = await container.delegations.dispatch(
            s,
            parent_id=parent.id,
            agent=reviewer,
            thread="review",
            prompt="check",
            call_id="k",
            suspends=False,
            awaited=False,
        )
    # 只跑运行、不投递任何事件：thread 做完、结清，发起方的运行接着处理交回的事件
    await run_only(container)

    assert any(f"thread:{child.id}" in p and "reviewed check" in p for p in recorder.prompts[-1])
    [hold] = await _holds(container, parent.id)
    assert hold.status == HoldStatus.RELEASED


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

    async def hold_then_stop(self, run_id, session_id, current):  # noqa: ANN001, ANN202
        # 运行刚退出、停止事务之前：模拟退出途中提交的占用（如后台任务的启动）
        async with container.db.transaction() as s:
            await hold_in(s, session_id, "job:77", awaited=False)
        return await original(self, run_id, session_id, current)

    consumer_module.Consumer._stopped = hold_then_stop
    try:
        worker = asyncio.create_task(container.worker.run_forever())
        await asyncio.wait_for(started.wait(), 5)
        await container.stopper(session.id)
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
    run = await container.queue.claim([RUN_TASK])
    assert run is not None

    # 领取之后、登记可停止的运行之前停止：停止直接执行停止事务
    await container.stopper(session.id)
    commit = await container.consumer.run(run.id, session.id)
    await container.queue.complete(run.id, commit)

    assert called == []


async def test_stopping_a_thread_the_user_already_deleted_is_not_an_error(container, make_agent, models):
    from muliao.sessions.halt import Halted

    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    await drain(container)
    await container.sessions.delete(session.id)
    assert await container.stopper(session.id) == Halted.GONE


async def test_stopping_a_thread_hands_back_at_once_and_deleting_its_agent_later_changes_nothing(
    container, make_agent, models
):
    """停止 thread 的事务里就把「被停止」交回发起方（带 thread 的 agent 名），之后删除这个 agent 不影响已交回的结局。"""
    lead = (await make_agent("lead", model="l")).id
    reviewer_id = (await make_agent("reviewer", model="v")).id
    reviewer = await container.agents.get(reviewer_id)
    recorder = Recorder()
    models.models["l"] = scripted(recorder)
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        await container.delegations.dispatch(
            s,
            parent_id=parent.id,
            agent=reviewer,
            thread="review",
            prompt="check",
            call_id="k",
            suspends=False,
            awaited=False,
        )
    [child] = await threads_of(container, parent.id)
    # thread 被停止：同一个事务里结清并交回
    await container.stopper(child.id)
    await container.agent_lifecycle.delete(reviewer_id, actor="user")

    await drain(container)

    async with container.db.transaction() as s:
        # thread 随 agent 删除，委派行随之删除，占用上的引用置空；占用本身留在发起方
        [hold] = (await s.scalars(select(SessionHold).where(SessionHold.session_id == parent.id))).all()
    assert hold.status == HoldStatus.RELEASED and hold.thread_call_id is None
    assert any("(reviewer) was stopped" in p for p in recorder.prompts[-1])


async def test_a_failing_run_failed_subscriber_rolls_back_the_settlement(container, make_agent, models):
    """RunFailed 的订阅者在失败收尾的事务里执行：它出错时收尾整个回滚，失败照记（不带收尾）；没有补做的对账，委派
    不结清、发起方的占用一直 active（要用户停止），这是删掉对账的代价。"""
    from muliao.sessions.events import RunFailed

    lead = (await make_agent("lead", model="l")).id
    reviewer = await container.agents.get((await make_agent("reviewer", model="v")).id)
    models.models["l"] = scripted(Recorder())

    def refuse(_messages, _info):  # noqa: ANN001, ANN202  契约外的失败：不重试
        raise ModelHTTPError(status_code=400, model_name="v", body="bad request")

    models.models["v"] = scripted(refuse)
    parent = await start(container, lead, "go")
    await drain(container)

    async def broken(_s, _event):  # noqa: ANN001, ANN202
        raise RuntimeError("subscriber broke")

    container.messaging.subscribe(RunFailed, broken)
    async with container.db.transaction() as s:
        child = await container.delegations.dispatch(
            s,
            parent_id=parent.id,
            agent=reviewer,
            thread="review",
            prompt="look",
            call_id="k",
            suspends=False,
            awaited=False,
        )
    await drain(container)

    async with container.db.transaction() as s:
        [run] = (
            await s.scalars(select(Task).where(Task.session_id == child.id, Task.kind == RUN_TASK))
        ).all()
    assert run.status == TaskStatus.FAILED
    assert [m for m in await _mail_of(container, parent.id) if m.source == f"thread:{child.id}"] == []
    async with container.db.transaction() as s:
        [hold] = (await s.scalars(select(SessionHold).where(SessionHold.thread_call_id.is_not(None)))).all()
    assert hold.status == HoldStatus.ACTIVE


async def test_stopping_a_thread_is_a_task_that_survives_a_restart(container, make_agent, models):
    """停下发起方时给 thread 排的停止任务与清掉占用同一事务提交：重启后照样恢复执行，thread 排着的运行不再执行。"""
    from muliao.sessions.lanes import STOP_THREAD_TASK
    from tests.conftest import restart
    from tests.test_flow import side_tasks

    lead = (await make_agent("lead", model="l")).id
    reviewer = await container.agents.get((await make_agent("reviewer", model="v")).id)
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        child = await container.delegations.dispatch(
            s,
            parent_id=parent.id,
            agent=reviewer,
            thread="review",
            prompt="look",
            call_id="k",
            suspends=False,
            awaited=False,
        )
    await container.stopper(parent.id)
    async with container.db.transaction() as s:
        [stop] = (await s.scalars(select(Task).where(Task.kind == STOP_THREAD_TASK))).all()
    assert (stop.session_id, stop.status) == (child.id, TaskStatus.PENDING)

    await restart(container)
    await side_tasks(container)
    async with container.db.transaction() as s:
        tasks = (await s.scalars(select(Task).where(Task.session_id == child.id))).all()
    assert {(t.kind, t.status) for t in tasks} == {
        (RUN_TASK, TaskStatus.DONE),
        (STOP_THREAD_TASK, TaskStatus.DONE),
    }
    assert not [m for m in await _mail_of(container, child.id) if m.state.value == "queued"]


async def _mail_of(container, session_id: str) -> list[Mail]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Mail).where(Mail.session_id == session_id))).all())


async def test_deleting_a_stopped_asker_drops_its_threads_outcome(container, make_agent, models):
    """停止过的发起方（占用 cleared）被删除，它开的 thread 保留：thread 的结清得到 DROPPED，没有失败。"""
    lead = (await make_agent("lead", model="l")).id
    reviewer = await container.agents.get((await make_agent("reviewer", model="v")).id)
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        child = await container.delegations.dispatch(
            s,
            parent_id=parent.id,
            agent=reviewer,
            thread="ask",
            prompt="check",
            call_id="c1",
            suspends=True,
            awaited=False,
        )
    await container.stopper(parent.id)
    await drain(container)
    await container.sessions.delete(parent.id)
    await drain(container)

    async with container.db.transaction() as s:
        from muliao.sessions.tables import Session

        kept = await s.get(Session, child.id)
        assert kept is not None and kept.parent_session_id is None
        assert (await s.scalars(select(Task).where(Task.status == TaskStatus.FAILED))).all() == []


async def test_stopping_a_thread_that_is_already_deleted_is_a_no_op(container, make_agent):
    from muliao.sessions.api import Halted

    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    await drain(container)
    await container.sessions.delete(session.id)

    assert await container.stopper(session.id) == Halted.GONE


async def test_thread_checks_leave_background_job_holds_alone(container, make_agent):
    agent = (await make_agent("alpha")).id
    session = await start(container, agent, "go")
    await drain(container)
    async with container.db.transaction() as s:
        await hold_in(s, session.id, "job:1", awaited=False)

    assert await container.thread_checks.check() == 0
