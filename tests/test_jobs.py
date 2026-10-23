"""后台任务：不占住这一轮、不设时限；结束、运行中检查都作为事件交给发起它的会话。"""

import asyncio
import os

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.errors import Conflict
from muliao.jobs.tables import Job, JobStatus
from muliao.sessions.api import Origin
from muliao.sessions.holds import has_active_in, hold_in
from muliao.sessions.mailbox import queued_in
from muliao.sessions.tables import Mail, MailState
from muliao.sessions.transcript import to_items
from tests.scripted import scripted
from tests.test_flow import deliver, drain, history_of, start
from tests.test_mailbox import Recorder


async def wait_until_done(container, job_id: int, seconds: float = 10) -> Job:
    for _ in range(int(seconds / 0.05)):
        async with container.db.transaction() as s:
            job = await s.get(Job, job_id)
        if job is not None and job.status != JobStatus.RUNNING:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


async def queued_events(container, session_id: str) -> list[Mail]:
    async with container.db.transaction() as s:
        return await queued_in(s, session_id)


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
    await deliver(container)
    # 上一条检查还在信箱里（还没交给模型）：会话领域不再堆一条
    assert await container.jobs.check() == 1
    await deliver(container)
    checks = [m for m in await queued_events(container, session.id) if m.source == f"job:{job.id}"]
    assert len(checks) == 1 and checks[0].check_in
    await drain(container)
    assert "still running" in recorder.prompts[-1][0] and "started" in recorder.prompts[-1][0]

    await container.jobs.kill(job.id, job.agent_id)
    killed = await wait_until_done(container, job.id)
    await drain(container)
    assert killed.status == JobStatus.KILLED
    assert "was killed" in recorder.prompts[-1][0]
    # 界面上检查与结局可以区分（显式标记，不靠文字）
    events = [i for i in to_items(await history_of(container, session.id)) if i.type == "event"]
    assert [(i.tool_name, i.check_in) for i in events] == [(f"job:{job.id}", True), (f"job:{job.id}", False)]
    assert all(i.at is not None for i in events)
    with pytest.raises(Conflict, match="not running"):
        await container.jobs.kill(job.id, job.agent_id)


async def test_jobs_left_running_by_a_previous_process_are_reported_interrupted(
    container, make_agent, models
):
    agent = await make_agent("alpha", model="rec")
    recorder = Recorder()
    models.models["rec"] = scripted(recorder)
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    await drain(container)
    async with container.db.transaction() as s:
        job = Job(
            session_id=session.id,
            agent_id=agent.id,
            command="sleep 100",
            secrets=[],
            log_path="jobs/x.log",
            pid=424242,
        )
        s.add(job)
    # 上一个进程启动它时已在会话上登记了占用（会话领域自己的事务），发起它的运行有用户在等
    async with container.db.transaction() as s:
        await hold_in(s, session.id, f"job:{job.id}", awaited=True)

    await container.jobs.recover()
    await drain(container)

    async with container.db.transaction() as s:
        [job] = (await s.scalars(select(Job))).all()
    assert job.status == JobStatus.INTERRUPTED
    [event_text] = recorder.prompts[-1]
    assert "interrupted" in event_text and "stopped unexpectedly" in event_text
    # 进程自成进程组、可能还活着：如实告诉 agent 怎么核实，不盲杀
    assert "pgrep -g 424242" in event_text
    # 发起它的运行有用户在等：处理中断通知的运行推送
    assert len(await container.notifications.list(100)) == 1


async def test_runs_without_a_session_get_no_background_jobs(container):
    """后台任务属于会话：临时 agent 用的那份工作区工具没有 background 参数，也没有 job 工具（靠不提供，不靠运行时检查）。"""
    from muliao.agent_runtime.workspace_tools import ShellLimits, workspace_toolset

    plain = workspace_toolset(container.vault, ShellLimits(10, 20, 1000), container.files)
    assert "job_status" not in plain.tools and "job_kill" not in plain.tools
    assert "background" not in plain.tools["run_shell"].function_schema.json_schema["properties"]
    session = container.consumer._tool_groups.groups["workspace"][0]  # noqa: SLF001
    assert {"job_status", "job_kill"} <= set(session.tools)
    assert "background" in session.tools["run_shell"].function_schema.json_schema["properties"]


async def test_a_session_with_a_running_job_cannot_be_deleted(container, make_agent, models):
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    with pytest.raises(Conflict, match="busy"):
        await container.sessions.delete(session.id)
    await container.jobs.kill(job.id, job.agent_id)
    await wait_until_done(container, job.id)
    await drain(container)
    await container.sessions.delete(session.id)


async def test_stopping_the_session_kills_its_jobs_without_waking_it_again(container, make_agent, models):
    session, job, recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    prompts = len(recorder.prompts)

    await container.stopper(session.id)
    # 后台任务领域收到 SessionStopped 后结束进程
    await drain(container)

    killed = await wait_until_done(container, job.id)
    await asyncio.sleep(0.2)  # 被一起停下的任务不再投信
    await drain(container)
    assert killed.status == JobStatus.KILLED
    assert len(recorder.prompts) == prompts
    async with container.db.transaction() as s:
        mail = (await s.scalars(select(Mail).where(Mail.session_id == session.id))).all()
    assert all(m.state != MailState.QUEUED for m in mail)


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
    import muliao.jobs.runner as jobs_module

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
    async def failing_finish(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    # 在开任务之前换掉：start_job 要跑完整轮运行，满载时比命令本身还慢，之后再换就赶不上它写结局（#157）
    monkeypatch.setattr(container.jobs, "_finish", failing_finish)
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "true")
    for _ in range(200):
        if job.id not in container.jobs._running:  # noqa: SLF001
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the supervisor never ended")
    monkeypatch.undo()

    await container.jobs.check()
    await deliver(container)

    async with container.db.transaction() as s:
        assert (await s.get(Job, job.id)).status == JobStatus.INTERRUPTED
    [event] = await queued_events(container, session.id)
    assert "supervision ended unexpectedly" in event.content


async def test_a_long_line_is_split_without_leaking_a_secret_or_breaking_characters(tmp_path):
    from muliao.jobs.process import _safe_cut

    text = "x" * 10 + "SECRET" + "y" * 10
    # 切口落在 secret 中间时挪到它前面
    assert _safe_cut(text, 13, {"K": "SECRET"}) == 10
    assert _safe_cut(text, 5, {"K": "SECRET"}) == 5


async def test_stopping_the_service_reports_jobs_as_interrupted(container, make_agent, models):
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    await container.jobs.stop()
    await deliver(container)
    async with container.db.transaction() as s:
        assert (await s.get(Job, job.id)).status == JobStatus.INTERRUPTED
    [event] = await queued_events(container, session.id)
    assert "the service was stopped" in event.content and "pgrep" not in event.content


async def test_a_check_while_a_job_records_its_outcome_does_not_report_it_interrupted(
    container, make_agent, models, monkeypatch
):
    """进程自然退出、结局正在写的那一刻碰上检查：监督者还登记着，对账不能抢先记成被中断。"""
    real_finish = container.jobs._finish  # noqa: SLF001
    writing = asyncio.Event()
    release = asyncio.Event()

    async def slow_finish(*args, **kwargs):
        writing.set()
        await release.wait()
        await real_finish(*args, **kwargs)

    # 在开任务之前换掉：start_job 要跑完整轮运行，满载时比命令本身还慢，之后再换就赶不上它写结局
    monkeypatch.setattr(container.jobs, "_finish", slow_finish)
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "true")
    await asyncio.wait_for(writing.wait(), 5)
    await container.jobs.check()
    release.set()
    finished = await wait_until_done(container, job.id)
    await deliver(container)

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
    from muliao.jobs.process import _safe_cut

    # 挪过一次之后，另一个 secret 跨到了新切口上，要继续挪
    text = "....ABCDEFGH...."
    # ABCD 先查时还没跨在切口 9 上；DEFGH 把切口挪到 7 之后它才跨上去
    assert _safe_cut(text, 9, {"B": "ABCD", "A": "DEFGH"}) == 4


async def _session_for_jobs(container, make_agent):
    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    return session, agent


async def _job_rows(container) -> list[Job]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Job))).all())


async def test_a_job_that_fails_before_holding_its_session_leaves_no_row(container, make_agent, monkeypatch):
    session, agent = await _session_for_jobs(container, make_agent)

    async def broken_hold(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(container.jobs._sessions, "hold", broken_hold)  # noqa: SLF001
    with pytest.raises(RuntimeError, match="database unavailable"):
        await container.jobs.start(
            session_id=session.id, agent_id=agent.id, command="true", secrets=[], awaited=False
        )
    assert await _job_rows(container) == []


async def _held_job_outcome(container, session_id: str) -> tuple[Job, str]:
    """启动失败、已经占用了会话的任务：按被中断结束，结局交回会话并释放占用。"""
    await deliver(container)
    [row] = await _job_rows(container)
    [event] = [m for m in await queued_events(container, session_id) if m.source == f"job:{row.id}"]
    async with container.db.transaction() as s:
        assert not await has_active_in(s, session_id)
    return row, event.content


async def test_a_job_whose_process_cannot_start_is_reported_interrupted(container, make_agent, monkeypatch):
    session, agent = await _session_for_jobs(container, make_agent)

    async def broken_spawn(*_args, **_kwargs):
        raise OSError("bash missing")

    monkeypatch.setattr("muliao.jobs.runner.spawn_bash", broken_spawn)
    with pytest.raises(OSError, match="bash missing"):
        await container.jobs.start(
            session_id=session.id, agent_id=agent.id, command="true", secrets=[], awaited=False
        )

    row, content = await _held_job_outcome(container, session.id)
    assert (
        row.status == JobStatus.INTERRUPTED
        and "could not be started" in content
        and "bash missing" in content
    )


async def test_a_job_whose_pid_cannot_be_recorded_kills_its_process_and_is_reported_interrupted(
    container, make_agent, monkeypatch
):
    session, agent = await _session_for_jobs(container, make_agent)
    spawned = []
    real_spawn = __import__("muliao.jobs.runner", fromlist=["spawn_bash"]).spawn_bash

    async def recording_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        spawned.append(proc)
        return proc

    async def broken_record(_self, _job, _pid):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("muliao.jobs.runner.spawn_bash", recording_spawn)
    monkeypatch.setattr("muliao.jobs.runner.JobRunner._record_pid", broken_record)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await container.jobs.start(
            session_id=session.id, agent_id=agent.id, command="sleep 30", secrets=[], awaited=False
        )

    [proc] = spawned
    await asyncio.wait_for(proc.wait(), 5)
    row, content = await _held_job_outcome(container, session.id)
    assert row.status == JobStatus.INTERRUPTED and "process id could not be recorded" in content


async def test_a_start_whose_cleanup_times_out_is_reconciled_as_interrupted(
    container, make_agent, monkeypatch
):
    session, agent = await _session_for_jobs(container, make_agent)

    async def broken_spawn(*_args, **_kwargs):
        raise OSError("bash missing")

    async def stuck_close(_self, _job_id, _reason):
        await asyncio.sleep(60)

    monkeypatch.setattr("muliao.jobs.runner.spawn_bash", broken_spawn)
    monkeypatch.setattr("muliao.jobs.runner.JobRunner._close_unstarted", stuck_close)
    monkeypatch.setattr("muliao.jobs.runner._CLEANUP_SECONDS", 0.1)
    with pytest.raises(OSError):
        await container.jobs.start(
            session_id=session.id, agent_id=agent.id, command="true", secrets=[], awaited=False
        )

    [row] = await _job_rows(container)
    assert row.status == JobStatus.RUNNING and row.pid is None
    await container.jobs.check()
    row, content = await _held_job_outcome(container, session.id)
    assert row.status == JobStatus.INTERRUPTED and "supervision ended unexpectedly" in content


async def test_leaving_the_running_service_interrupts_jobs_after_the_runs_have_stopped(settings, models):
    """Assembly.running 的退出顺序：先取消运行与循环，再结束后台任务的进程并记为被中断。"""
    from muliao.agents.service import AgentSpec
    from muliao.container import assemble

    c = assemble(settings, resolve_model=models, model_checker=lambda _m: None)
    async with c.running():
        agent = await c.agents.create(
            AgentSpec(name="alpha", description="d", model="m"),
            author="user",
        )
        session = await start(c, agent.id, "hi", origin=Origin.TRIGGER)
        job = await c.jobs.start(
            session_id=session.id, agent_id=agent.id, command="sleep 30", secrets=[], awaited=False
        )

    async with c.db.transaction() as s:
        row = await s.get(Job, job.id)
    await c.db.engine.dispose()
    assert row.status == JobStatus.INTERRUPTED
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(job.pid, 0)


async def test_a_check_while_a_job_is_starting_skips_it(container, make_agent, monkeypatch):
    import anyio

    session, agent = await _session_for_jobs(container, make_agent)
    holding, release = anyio.Event(), anyio.Event()
    launch = container.jobs._launch

    async def slow_launch(*args, **kwargs):
        # 行已插入、占用还没登记（hold_in 在 _launch 的第一个事务里）；停在事务之外
        holding.set()
        await release.wait()
        await launch(*args, **kwargs)

    monkeypatch.setattr(container.jobs, "_launch", slow_launch)
    async with anyio.create_task_group() as tg:
        tg.start_soon(
            lambda: container.jobs.start(
                session_id=session.id, agent_id=agent.id, command="true", secrets=[], awaited=False
            )
        )
        await holding.wait()
        # 行已经是 running、占用还没登记：既不当成没人监督的，也不发检查
        assert await container.jobs.check() == 0
        release.set()
    [row] = await _job_rows(container)
    assert (await wait_until_done(container, row.id)).status == JobStatus.EXITED


async def test_a_job_whose_session_was_deleted_is_kept_as_interrupted_after_a_crash(container, make_agent):
    """占用行随会话删除：会话已不在时不能把运行过的任务当成「没开始过」删掉，记为被中断，交回得到「会话已不在」。"""
    from tests.test_flow import drain

    session, agent = await _session_for_jobs(container, make_agent)
    async with container.db.transaction() as s:
        job = Job(
            session_id=session.id,
            agent_id=agent.id,
            command="sleep 100",
            secrets=[],
            log_path="jobs/x.log",
            pid=424242,
        )
        s.add(job)
    await drain(container)
    await container.sessions.delete(session.id)

    await container.jobs.recover()
    await drain(container)

    [row] = await _job_rows(container)
    assert row.status == JobStatus.INTERRUPTED
    async with container.db.transaction() as s:
        from muliao.platform.tables import Task, TaskStatus

        assert (await s.scalars(select(Task).where(Task.status == TaskStatus.FAILED))).all() == []


async def test_a_job_that_ends_while_a_check_is_being_prepared_gets_no_check(container, make_agent, models):
    """检查读快照之后、发出之前任务结束：确认仍在运行的事务看到它已结束，不发检查；信箱里只有完成事件。"""
    import threading

    from muliao.jobs import runner as runner_module

    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    real_tail = runner_module.tail
    entered, gate = threading.Event(), threading.Event()
    calls = {"n": 0}

    def slow_tail(path):  # noqa: ANN001, ANN202  只拦住检查读的第一次，结束时读最后几行照常
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            gate.wait(10)
        return real_tail(path)

    runner_module.tail = slow_tail
    try:
        check = asyncio.create_task(container.jobs.check())
        await asyncio.to_thread(entered.wait, 5)
        await container.jobs.kill(job.id, job.agent_id)
        await wait_until_done(container, job.id)
        gate.set()
        assert await check == 0
    finally:
        gate.set()
        runner_module.tail = real_tail
    await deliver(container)

    events = [m.content for m in await queued_events(container, session.id) if m.source == f"job:{job.id}"]
    assert len(events) == 1 and "was killed" in events[0]


async def test_a_check_sent_before_the_job_ends_arrives_before_its_end(container, make_agent, models):
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")

    assert await container.jobs.check() == 1
    await container.jobs.kill(job.id, job.agent_id)
    await wait_until_done(container, job.id)
    await deliver(container)

    events = [m.content for m in await queued_events(container, session.id) if m.source == f"job:{job.id}"]
    assert len(events) == 2 and "still running" in events[0] and "was killed" in events[1]


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()


async def test_a_multiline_secret_across_a_read_boundary_never_reaches_the_log(tmp_path):
    from muliao.jobs.process import _CHUNK, _record

    pem = "-----BEGIN KEY-----\nAAAA\nBBBB\n-----END KEY-----"
    proc = _FakeProc()
    # secret 中间的换行恰好落在第一块里，其余部分在下一块
    data = "x" * (_CHUNK - 25) + pem + "\ndone\n"
    proc.stdout.feed_data(data.encode())
    proc.stdout.feed_eof()
    log_file = tmp_path / "out.log"
    await _record(proc, log_file, {"PEM": pem})
    assert log_file.read_text() == "x" * (_CHUNK - 25) + "[secret:PEM]\ndone\n"


async def test_output_read_before_a_cancelled_drain_is_written_without_a_secret_prefix(tmp_path):
    from muliao.jobs.process import _record

    proc = _FakeProc()
    # 没有换行的最后一段也要写出；末尾的 "se" 可能是 secret 的开头，留着不写
    proc.stdout.feed_data(b"line1\nno newline yet se")
    log_file = tmp_path / "out.log"
    task = asyncio.create_task(_record(proc, log_file, {"K": "sec"}))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log_file.read_text() == "line1\nno newline yet "


async def test_output_of_a_quiet_job_reaches_the_log_even_with_long_secrets(tmp_path):
    """只留着可能是 secret 开头的那一段，不按最长 secret 的长度一律扣住末尾：否则安静下来的进程最后几行看不到。"""
    from muliao.jobs.process import _record

    proc = _FakeProc()
    proc.stdout.feed_data(b"starting\nlistening on http://localhost:8080\n")
    log_file = tmp_path / "out.log"
    task = asyncio.create_task(_record(proc, log_file, {"K": "k" * 40}))
    await asyncio.sleep(0.1)
    assert log_file.read_text() == "starting\nlistening on http://localhost:8080\n"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_an_orphan_that_ends_while_its_log_is_read_is_left_alone(
    container, make_agent, models, monkeypatch
):
    """对账在事务外读日志（_close_each）：读日志期间候选任务的状态变了（这里由别处记为结束），写事务里重新选取时
    就不再收尾它——不改它的结局、不删它的行、不投「被中断」。"""
    import threading

    from muliao.jobs import runner as runner_module

    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    await container.jobs.kill(job.id, job.agent_id)
    await wait_until_done(container, job.id)
    await deliver(container)
    # 让它看起来像监督意外结束的任务：库里还是 running，本进程没人监督
    async with container.db.transaction() as s:
        (await s.get(Job, job.id)).status = JobStatus.RUNNING
    real_tail = runner_module.tail
    entered, gate = threading.Event(), threading.Event()

    def slow_tail(path):  # noqa: ANN001, ANN202
        entered.set()
        gate.wait(10)
        return real_tail(path)

    monkeypatch.setattr(runner_module, "tail", slow_tail)
    try:
        check = asyncio.create_task(container.jobs.check())
        assert await asyncio.to_thread(entered.wait, 5)
        async with container.db.transaction() as s:
            (await s.get(Job, job.id)).status = JobStatus.KILLED
        gate.set()
        assert await check == 0
    finally:
        gate.set()
    await deliver(container)

    async with container.db.transaction() as s:
        assert (await s.get(Job, job.id)).status == JobStatus.KILLED
    events = [m.content for m in await queued_events(container, session.id) if m.source == f"job:{job.id}"]
    assert not any("interrupted" in e for e in events)
