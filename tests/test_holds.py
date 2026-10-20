"""占用与结清（runtime.holds、runtime.settlement，docs/domain-design.md 4.1、4.2）：会话只看自己的数据判断工作是否
做完、什么时候结清；委派的结局只交回一次；停止之后迟到的结局不投。"""

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.errors import InvalidInput
from muliao.models import Delegation, DelegationStatus, HoldStatus, Mail, SessionHold, Task, TaskKind
from muliao.runtime.holds import hold_in
from muliao.runtime.settlement import Delivered, deliver_in
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
    from muliao.runtime.conversation import Threads

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
    async with container.db.transaction() as s:
        events = (await s.scalars(select(Task).where(Task.kind == TaskKind.EVENT))).all()
    assert events == []


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
