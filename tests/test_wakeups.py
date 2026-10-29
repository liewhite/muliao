"""到点唤醒（wake_me，sessions.wakeups）：约好后会话在等（waiting），到点投一封带说明的事件信件、释放占用、接着做；
改期取代、可以取消；会话被停止时取消；存在库里，重启后照样到点投。"""

import asyncio
from datetime import timedelta

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.platform.db import utcnow
from muliao.platform.tool_views import NO_RENDERS
from muliao.sessions.activity import SessionActivity, WorkState
from muliao.sessions.sources import OtherSource
from muliao.sessions.tables import HoldStatus, Mail, SessionHold, WakeUp
from muliao.sessions.transcript import to_items
from muliao.sessions.wakeups import WakeUps
from tests.scripted import scripted
from tests.test_flow import drain, history_of, start
from tests.test_mailbox import Recorder
from tests.test_tools import run_tools


async def _state(container, session_id: str) -> WorkState:
    async with container.db.transaction() as s:
        return await SessionActivity(container.queue).work_state_in(s, session_id)


async def _holds(container, session_id: str) -> list[SessionHold]:
    async with container.db.transaction() as s:
        return list(
            (
                await s.scalars(
                    select(SessionHold).where(SessionHold.session_id == session_id).order_by(SessionHold.id)
                )
            ).all()
        )


def _wakeups_later(container) -> WakeUps:
    """时钟拨快 2 小时的 WakeUps（同一个库，等于重启后的新实例）。"""
    return WakeUps(
        container.db, container.queue, container.wakeups._tz, clock=lambda: utcnow() + timedelta(hours=2)
    )


async def _schedule(container, make_agent, models, args: dict):
    """agent 在一次运行里调一次 wake_me 然后停下；返回会话与记录之后模型输入的 Recorder。"""
    agent = await make_agent("alpha", model="w")
    recorder = Recorder()
    calls = {"n": 0}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[ToolCallPart("wake_me", args, tool_call_id="w1")])
        if calls["n"] == 2:
            return ModelResponse(parts=[TextPart("see you later")])
        return recorder(messages, info)

    models.models["w"] = scripted(fn)
    session = await start(container, agent.id, "deploy and check later")
    await drain(container)
    return session, recorder


async def test_a_due_wake_up_wakes_the_session_with_its_note(container, make_agent, models):
    session, recorder = await _schedule(
        container, make_agent, models, {"after_seconds": 3600, "note": "check the deploy"}
    )
    # 约好之后：会话在等（不是 idle），没到点不投
    assert await _state(container, session.id) == WorkState.WAITING
    assert await container.wakeups.check() == 0

    # 用注入的时钟到点（真实 sleep 的短间隔在并行跑测试时会被 drain 的耗时超过，没到点的检查就投了）
    later = _wakeups_later(container)
    assert await later.check() == 1
    # 已投过：不再投
    assert await later.check() == 0
    await drain(container)

    [event_text] = recorder.prompts[-1]
    assert 'source="wake_me"' in event_text and "Your note: check the deploy" in event_text
    [hold] = await _holds(container, session.id)
    assert hold.status == HoldStatus.RELEASED
    assert await _state(container, session.id) == WorkState.IDLE
    # 用户在等约它的那次运行的回答：唤醒的信件随之推送
    async with container.db.transaction() as s:
        [mail] = (
            await s.scalars(select(Mail).where(Mail.session_id == session.id, Mail.source == "wake_me"))
        ).all()
    assert mail.awaited
    events = [i for i in to_items(await history_of(container, session.id), NO_RENDERS) if i.type == "event"]
    assert [i.source for i in events] == [OtherSource(kind="other", raw="wake_me")]


async def test_stopping_the_session_cancels_its_wake_up(container, make_agent, models):
    session, recorder = await _schedule(container, make_agent, models, {"after_seconds": 0.05, "note": "n"})
    await container.sessions.stop(session.id)
    assert await _state(container, session.id) == WorkState.IDLE
    await asyncio.sleep(0.1)
    assert await container.wakeups.check() == 0
    await drain(container)
    assert recorder.prompts == []
    [hold] = await _holds(container, session.id)
    assert hold.status == HoldStatus.CLEARED


async def test_a_wake_up_survives_a_restart(container, make_agent, models):
    """到没到点只看库里的 due_at 与占用：重启后新建的 WakeUps 照样投。"""
    session, recorder = await _schedule(
        container, make_agent, models, {"after_seconds": 3600, "note": "later"}
    )
    restarted = _wakeups_later(container)
    assert await restarted.check() == 1
    await drain(container)
    assert "Your note: later" in recorder.prompts[-1][0]


async def test_scheduling_again_replaces_and_cancel_removes(container, make_agent, models):
    agent = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        agent.id,
        [
            ("wake_me", {"at": "2099-01-01T10:00", "note": "first"}),
            ("wake_me", {"after_seconds": 3600, "note": "second"}),
            ("wake_me", {"cancel": True}),
            ("wake_me", {"cancel": True}),
            ("wake_me", {"after_seconds": 60, "note": "third"}),
        ],
    )
    assert outputs[0].startswith("scheduled wake-up at 2099-01-01 10:00 ")
    assert (
        outputs[1].startswith("scheduled wake-up at ")
        and "; replaced the one at 2099-01-01 10:00" in outputs[1]
    )
    assert outputs[2].startswith("cancelled wake-up at ")
    assert outputs[3] == "no wake-up was scheduled; nothing to cancel"
    async with container.db.transaction() as s:
        [session_id] = set((await s.scalars(select(WakeUp.session_id))).all())
    holds = await _holds(container, session_id)
    assert [h.status for h in holds] == [HoldStatus.CLEARED, HoldStatus.CLEARED, HoldStatus.ACTIVE]
    assert await _state(container, session_id) == WorkState.WAITING


async def test_wrong_arguments_are_handed_back(container, make_agent, models):
    agent = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        agent.id,
        [
            ("wake_me", {"note": "n"}),
            ("wake_me", {"note": "n", "at": "2099-01-01T10:00", "after_seconds": 5}),
            ("wake_me", {"note": "n", "at": "2001-01-01T10:00"}),
            ("wake_me", {"note": "n", "at": "tomorrow"}),
            ("wake_me", {"note": "n", "after_seconds": 0}),
            ("wake_me", {"after_seconds": 5}),
            ("wake_me", {"cancel": True, "note": "n"}),
        ],
    )
    assert "exactly one of `at` and `after_seconds`" in outputs[0] and "exactly one" in outputs[1]
    assert "is in the past" in outputs[2]
    assert "ISO 8601" in outputs[3]
    assert "greater than 0" in outputs[4]
    assert "`note` is required" in outputs[5]
    assert "takes no other arguments" in outputs[6]
    async with container.db.transaction() as s:
        assert (await s.scalars(select(WakeUp))).all() == []


async def test_a_wake_up_too_far_away_is_handed_back(container, make_agent, models):
    agent = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container, models, agent.id, [("wake_me", {"note": "n", "after_seconds": 1e20})]
    )
    assert "too far in the future" in outputs[0]
