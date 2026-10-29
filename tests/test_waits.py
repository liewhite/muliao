"""等待项（sessions.waits，#298）：会话页列出会话为什么在 waiting——后台任务、委派、到点唤醒、提问，来源与工作状态
同一条规则；后台任务补上命令与日志，可以从会话页结束。"""

from datetime import timedelta

import pytest
from pydantic_ai.messages import ModelResponse, TextPart

from muliao.platform.db import utcnow
from muliao.sessions.activity import Waiting, WorkState, waiting_among
from muliao.sessions.delegations import record_in
from muliao.sessions.holds import hold_in
from muliao.sessions.questions import Option, Question
from muliao.sessions.waits import AskWait, DelegationWait, JobWait, WakeWait, waits_of_in
from muliao.sessions.wakeups import schedule_in
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, start
from tests.test_jobs import start_job, wait_until_done


async def _idle_session(container, make_agent, models):
    agent = await make_agent("alpha", model="t")
    models.models["t"] = scripted(lambda messages, info: ModelResponse(parts=[TextPart("ok")]))
    session = await start(container, agent.id, "hi")
    await drain(container)
    return session


def _question(header: str) -> Question:
    return Question(question=f"{header}?", header=header, options=[Option(label="a"), Option(label="b")])


async def test_each_kind_of_wait_is_named_in_the_order_it_began(container, make_agent, models):
    session = await _idle_session(container, make_agent, models)
    # 委派记录只要求 thread 是个会话：这里借会话自己
    thread = session
    due = utcnow() + timedelta(hours=1)
    async with container.db.transaction() as s:
        await hold_in(s, session.id, "job:7", awaited=False)
        await hold_in(s, session.id, "delegation:x:r", awaited=False)
        await record_in(
            s,
            holder="delegation:x:r",
            parent_id=session.id,
            thread_id=thread.id,
            check_seconds=30,
            timeout_seconds=None,
        )
        await hold_in(s, session.id, "delegation:x:a", awaited=False, call_id="a")
        await record_in(
            s,
            holder="delegation:x:a",
            parent_id=session.id,
            thread_id=thread.id,
            check_seconds=None,
            timeout_seconds=600,
        )
        await schedule_in(s, session.id, "w1", due, "check the deploy", awaited=False)
        [waiting] = (await waiting_among(s, [session.id])).values()
        waits = await waits_of_in(s, Waiting(waiting.holds, {"q1": [_question("Scope"), _question("When")]}))

    job, report, ask, wake, question = waits
    assert isinstance(job, JobWait) and job.job_id == 7
    assert isinstance(report, DelegationWait)
    assert (report.thread_session_id, report.check_seconds, report.deadline) == (thread.id, 30, None)
    assert isinstance(ask, DelegationWait) and ask.check_seconds is None and ask.deadline is not None
    assert isinstance(wake, WakeWait) and (wake.due_at, wake.note) == (due, "check the deploy")
    assert question == AskWait(call_id="q1", headers=["Scope", "When"])
    assert all(w.since is not None for w in (job, report, ask, wake))

    # 会话详情：同一事务里的工作状态与等待项（真实的历史里没有挂起的提问）
    view = await container.sessions.detail(session.id)
    assert view.state == WorkState.WAITING
    assert [w.kind for w in view.waits] == ["job", "delegation", "delegation", "wake"]


async def test_an_idle_session_waits_for_nothing(container, make_agent, models):
    session = await _idle_session(container, make_agent, models)
    view = await container.sessions.detail(session.id)
    assert (view.state, view.waits) == (WorkState.IDLE, [])


async def test_a_hold_of_no_known_kind_is_a_defect(container, make_agent, models):
    session = await _idle_session(container, make_agent, models)
    async with container.db.transaction() as s:
        await hold_in(s, session.id, "mystery:1", awaited=False)
        [waiting] = (await waiting_among(s, [session.id])).values()
        with pytest.raises(RuntimeError, match="mystery:1"):
            await waits_of_in(s, waiting)


async def test_a_background_job_is_shown_with_its_command_and_can_be_killed_from_the_page(
    container, make_agent, models, client
):
    session, job, _recorder, agent = await start_job(container, make_agent, models, "echo started; sleep 30")
    await login(client)

    detail = (await client.get(f"/api/sessions/{session.id}")).json()
    assert detail["state"] == "waiting"
    [wait] = detail["waits"]
    assert (wait["kind"], wait["job_id"], wait["command"]) == ("job", job.id, "echo started; sleep 30")
    assert wait["log_path"] == job.log_path

    resp = await client.post(f"/api/agents/{agent.id}/jobs/{job.id}/kill")
    assert resp.status_code == 202
    await wait_until_done(container, job.id)
    await drain(container)
    detail = (await client.get(f"/api/sessions/{session.id}")).json()
    assert detail["waits"] == []
    # 已经结束的不能再结束
    assert (await client.post(f"/api/agents/{agent.id}/jobs/{job.id}/kill")).status_code == 409
