"""后台任务：不占住这一轮、不设时限；结束、运行中检查都作为事件交给发起它的会话。"""

import asyncio
import os

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.errors import Conflict, InvalidInput
from muliao.models import Job, JobStatus, Origin, ReplyTo, SessionEvent, TaskKind
from tests.scripted import scripted
from tests.test_events import Recorder
from tests.test_flow import drain, start, tasks_of


async def wait_until_done(container, job_id: int, seconds: float = 10) -> Job:
    for _ in range(int(seconds / 0.05)):
        async with container.db.transaction() as s:
            job = await s.get(Job, job_id)
        if job is not None and job.status != JobStatus.RUNNING:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


async def queued_events(container, session_id: str) -> list[SessionEvent]:
    async with container.db.transaction() as s:
        rows = await s.scalars(
            select(SessionEvent).where(
                SessionEvent.session_id == session_id, SessionEvent.consumed_by.is_(None)
            )
        )
        return list(rows.all())


async def start_job(container, make_agent, models, command: str, secrets: tuple[str, ...] = ()):
    """让 agent 在一轮里开一个后台任务；返回会话、任务与记录模型输入的 Recorder。"""
    agent = await make_agent("alpha", model="bg")
    recorder = Recorder()
    calls = {"n": 0}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            args = {"command": command, "background": True, "secrets": list(secrets)}
            return ModelResponse(parts=[ToolCallPart("run_shell", args, tool_call_id="bg")])
        return recorder(messages, info)

    models.models["bg"] = scripted(fn)
    session = await start(container, agent.id, "build it", origin=Origin.TRIGGER)
    await drain(container)
    async with container.db.transaction() as s:
        [job] = (await s.scalars(select(Job).where(Job.session_id == session.id))).all()
    return session, job, recorder, agent


async def test_a_finished_background_job_reports_back_with_its_output(container, make_agent, models):
    session, job, recorder, agent = await start_job(
        container, make_agent, models, "echo building; echo done >&2; exit 3"
    )

    finished = await wait_until_done(container, job.id)
    await drain(container)

    assert (finished.status, finished.exit_code) == (JobStatus.EXITED, 3)
    [event_text] = recorder.prompts[-1]
    assert f'source="job:{job.id}"' in event_text and "exit code 3" in event_text
    assert "building" in event_text and "done" in event_text and f"jobs/{job.id}.log" in event_text
    log = (container.agents.workspace(agent.id) / job.log_path).read_text()
    assert log.split() == ["building", "done"]


async def test_secrets_never_reach_the_log(container, make_agent, models):
    await container.vault.set(None, "TOKEN", "s3cr3t-value")
    _session, job, _recorder, agent = await start_job(
        container, make_agent, models, 'echo "token is $TOKEN"', secrets=("TOKEN",)
    )
    await wait_until_done(container, job.id)
    log = (container.agents.workspace(agent.id) / job.log_path).read_text()
    assert "s3cr3t-value" not in log and "[secret:TOKEN]" in log


async def test_running_jobs_get_check_events_without_piling_up_and_can_be_killed(
    container, make_agent, models
):
    session, job, recorder, _agent = await start_job(container, make_agent, models, "echo started; sleep 30")

    # 输出写进日志之后再检查
    await asyncio.sleep(0.3)
    assert await container.jobs.check() == 1
    # 上一条检查还在队列里（这里还没执行那一轮）：不再堆一条
    assert await container.jobs.check() == 0
    await drain(container)
    assert "still running" in recorder.prompts[-1][0] and "started" in recorder.prompts[-1][0]

    await container.jobs.kill(job.id, job.agent_id)
    killed = await wait_until_done(container, job.id)
    await drain(container)
    assert killed.status == JobStatus.KILLED
    assert "was killed" in recorder.prompts[-1][0]
    with pytest.raises(Conflict, match="not running"):
        await container.jobs.kill(job.id, job.agent_id)


async def test_jobs_left_running_by_a_previous_process_are_reported_interrupted(
    container, make_agent, models
):
    agent = await make_agent("alpha", model="rec")
    models.models["rec"] = scripted(Recorder())
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    await drain(container)
    async with container.db.transaction() as s:
        s.add(
            Job(
                session_id=session.id,
                agent_id=agent.id,
                command="sleep 100",
                secrets=[],
                log_path="jobs/x.log",
                reply_to=ReplyTo.USER,
                pid=424242,
            )
        )

    await container.jobs.recover()

    async with container.db.transaction() as s:
        [job] = (await s.scalars(select(Job))).all()
    assert job.status == JobStatus.INTERRUPTED
    [event] = await queued_events(container, session.id)
    assert "interrupted" in event.content and "stopped unexpectedly" in event.content
    # 进程自成进程组、可能还活着：如实告诉 agent 怎么核实，不盲杀
    assert "pgrep -g 424242" in event.content
    await drain(container)
    # 完成通知随发起它的那一轮推送
    assert len(await tasks_of(container, TaskKind.DELIVER)) == 1


async def test_background_jobs_need_a_conversation_to_report_to(container, make_agent):
    agent = await make_agent("alpha")
    with pytest.raises(InvalidInput, match="conversation"):
        await container.jobs.start(
            session_id=None, agent_id=agent.id, command="true", secrets=[], reply_to=None
        )
    with pytest.raises(InvalidInput, match="private thread"):
        await container.jobs.start(
            session_id="s", agent_id=agent.id, command="true", secrets=[], reply_to=ReplyTo.AGENT
        )


async def test_a_session_with_a_running_job_cannot_be_deleted(container, make_agent, models):
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    with pytest.raises(Conflict, match="background jobs running"):
        await container.sessions.delete(session.id)
    await container.jobs.kill(job.id, job.agent_id)
    await wait_until_done(container, job.id)
    await drain(container)
    await container.sessions.delete(session.id)


async def test_stopping_the_service_kills_running_jobs(container, make_agent, models):
    _session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    async with container.db.transaction() as s:
        pid = (await s.get(Job, job.id)).pid

    await container.jobs.stop()

    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


async def test_background_runs_have_no_timeout_argument(container, make_agent, models):
    from tests.test_tools import run_tools

    agent = await make_agent("alpha", model="tools")
    [output] = await run_tools(
        container, models, agent.id, [("run_shell", {"command": "true", "background": True, "timeout": 5})]
    )
    assert "no timeout" in output


async def test_a_job_ends_when_its_process_exits_even_if_a_grandchild_keeps_the_output_open(
    container, make_agent, models, monkeypatch
):
    import muliao.runtime.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "DRAIN_SECONDS", 0.3)
    # 留在后台的孙进程继承了输出管道：等管道关闭就要等它 30 秒
    _session, job, recorder, _agent = await start_job(
        container, make_agent, models, "echo main; sleep 30 & exit 0"
    )
    finished = await wait_until_done(container, job.id)
    await drain(container)
    assert (finished.status, finished.exit_code) == (JobStatus.EXITED, 0)
    assert "still hold its output" in recorder.prompts[-1][0]


async def test_a_job_whose_outcome_could_not_be_recorded_is_reconciled(
    container, make_agent, models, monkeypatch
):
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 0.5")

    async def failing_finish(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(container.jobs, "_finish", failing_finish)
    for _ in range(100):
        if job.id not in container.jobs._running:  # noqa: SLF001
            break
        await asyncio.sleep(0.05)
    monkeypatch.undo()

    await container.jobs.check()

    async with container.db.transaction() as s:
        assert (await s.get(Job, job.id)).status == JobStatus.INTERRUPTED
    [event] = await queued_events(container, session.id)
    assert "supervision ended unexpectedly" in event.content


async def test_a_long_line_is_split_without_leaking_a_secret_or_breaking_characters(tmp_path):
    from muliao.runtime.jobs import _safe_cut

    text = "x" * 10 + "SECRET" + "y" * 10
    # 切口落在 secret 中间时挪到它前面
    assert _safe_cut(text, 13, {"K": "SECRET"}) == 10
    assert _safe_cut(text, 5, {"K": "SECRET"}) == 5


async def test_stopping_the_service_reports_jobs_as_interrupted(container, make_agent, models):
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    await container.jobs.stop()
    async with container.db.transaction() as s:
        assert (await s.get(Job, job.id)).status == JobStatus.INTERRUPTED
    [event] = await queued_events(container, session.id)
    assert "the service was stopped" in event.content and "pgrep" not in event.content


async def test_a_check_while_a_job_records_its_outcome_does_not_report_it_interrupted(
    container, make_agent, models, monkeypatch
):
    """进程自然退出、结局正在写的那一刻碰上检查：监督者还登记着，对账不能抢先记成被中断。"""
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 0.3")
    real_finish = container.jobs._finish  # noqa: SLF001
    writing = asyncio.Event()
    release = asyncio.Event()

    async def slow_finish(*args, **kwargs):
        writing.set()
        await release.wait()
        await real_finish(*args, **kwargs)

    monkeypatch.setattr(container.jobs, "_finish", slow_finish)
    await asyncio.wait_for(writing.wait(), 5)
    await container.jobs.check()
    release.set()
    finished = await wait_until_done(container, job.id)

    assert finished.status == JobStatus.EXITED
    contents = [e.content for e in await queued_events(container, session.id)]
    assert not any("interrupted" in c for c in contents)
    assert sum("finished with exit code 0" in c for c in contents) == 1


async def test_killing_a_job_that_just_exited_is_a_clear_conflict(container, make_agent, models):
    _session, job, _recorder, _agent = await start_job(container, make_agent, models, "true")
    await wait_until_done(container, job.id)
    with pytest.raises(Conflict):
        await container.jobs.kill(job.id, job.agent_id)


def test_the_cut_also_avoids_secrets_overlapping_each_other():
    from muliao.runtime.jobs import _safe_cut

    # 挪过一次之后，另一个 secret 跨到了新切口上，要继续挪
    text = "....ABCDEFGH...."
    # ABCD 先查时还没跨在切口 9 上；DEFGH 把切口挪到 7 之后它才跨上去
    assert _safe_cut(text, 9, {"B": "ABCD", "A": "DEFGH"}) == 4
