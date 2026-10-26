"""删除经事件完成（docs/architecture.md 第 10 节与第 7 节「thread 与结果路由」）：没有跨领域外键，各领域按 SessionDeleted / AgentDeleting 清理
自己的数据；先停止再删除时迟到的结局不产生失败投递。"""

import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import muliao.agents.service
from muliao.agents.api import McpTransport, mcp_config
from muliao.agents.tables import Agent
from muliao.errors import Conflict, NotFound
from muliao.jobs.tables import Job, JobStatus
from muliao.platform.tables import EVENT_TASK, Task, TaskStatus
from muliao.sessions.api import Origin
from muliao.sessions.inbound import Inbound, NewSession
from muliao.sessions.lanes import RUN_TASK, session_lane
from tests.test_flow import deliver, drain, start
from tests.test_jobs import start_job, wait_until_done


async def _failed_tasks(container) -> list[Task]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Task).where(Task.status == TaskStatus.FAILED))).all())


async def _lanes_of(container, *receivers: str) -> set[str]:
    """这些接收方排队中的投递所在的 lane（每个接收方都必须有一条）。"""
    async with container.db.transaction() as s:
        pending = (
            await s.scalars(select(Task).where(Task.kind == EVENT_TASK, Task.status == TaskStatus.PENDING))
        ).all()
    mine = [t for t in pending if t.payload["receiver"] in receivers]
    assert {t.payload["receiver"] for t in mine} == set(receivers)
    return {t.lane for t in mine}


async def test_stopping_then_deleting_right_away_leaves_no_failed_delivery(container, make_agent, models):
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")

    await container.stopper(session.id)
    # 停止之后会话不忙（占用已清掉），立刻可以删除；后台任务的进程随后才被结束、结局才写进它自己的行
    await container.sessions.delete(session.id)
    await drain(container)
    ended = await wait_until_done(container, job.id)
    await drain(container)

    assert ended.status == JobStatus.KILLED
    assert await _failed_tasks(container) == []
    async with container.db.transaction() as s:
        # 任务行保留终态（没有跨领域外键，不随会话删除）
        assert (await s.get(Job, job.id)) is not None


async def test_deleting_an_agent_without_sessions_completes(container, make_agent):
    agent = await make_agent("alpha")
    await container.agent_lifecycle.delete(agent.id, author="user")
    await drain(container)

    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    assert await _failed_tasks(container) == []


async def test_a_workspace_that_cannot_be_fully_removed_does_not_block_deleting_the_agent(
    container, make_agent, caplog, monkeypatch
):
    def rmtree_fails(path):  # noqa: ANN001, ANN202
        # 例如它留在后台的进程还在往里写
        raise OSError(39, "Directory not empty", str(path))

    # 只换掉 agent 领域删工作目录用的 rmtree（资源仓库照常删）
    monkeypatch.setattr(muliao.agents.service, "shutil", SimpleNamespace(rmtree=rmtree_fails))
    agent = await make_agent("alpha")
    container.agents.workspace(agent.id).mkdir(parents=True, exist_ok=True)

    await container.agent_lifecycle.delete(agent.id, author="user")
    await drain(container)

    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    assert await _failed_tasks(container) == []
    assert "workspace of a deleted agent could not be fully removed" in caplog.text
    # 残留由启动检查列进日志
    caplog.clear()
    await container.agents.verify_stored()
    [leftover] = [r for r in caplog.records if r.getMessage() == "workspaces of deleted agents remain"]
    assert leftover.agent_ids == [agent.id]


async def test_the_startup_check_does_not_report_an_agent_still_being_deleted(container, make_agent, caplog):
    agent = await make_agent("alpha")
    container.agents.workspace(agent.id).mkdir(parents=True, exist_ok=True)
    # 只标记、不投递：它的目录还在是正常的，删除流程会删
    await container.agent_lifecycle.delete(agent.id, author="user")

    await container.agents.verify_stored()

    assert "deleted agents remain" not in caplog.text


async def test_deleting_an_agent_removes_its_sessions_triggers_and_secrets(container, make_agent):
    agent = await make_agent("alpha")
    await container.vault.set(agent.id, "TOKEN", "x")
    await container.triggers.create(
        name="nightly", agent_id=agent.id, kind="schedule", prompt="p", config={"cron": "0 3 * * *"}
    )
    session = await start(container, agent.id, "hi")
    await drain(container)

    await container.agent_lifecycle.delete(agent.id, author="user")
    # 删除进行中：不再出现在列表与名称解析里
    assert agent.id not in [a.id for a in await container.agents.list()]
    with pytest.raises(NotFound):
        await container.agents.find_by_name("alpha")
    await drain(container)

    assert await container.triggers.list() == []
    assert await container.vault.view(agent.id) == []
    with pytest.raises(NotFound):
        await container.sessions.get(session.id)
    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    assert await _failed_tasks(container) == []


async def test_an_agent_marked_for_deletion_receives_no_new_mail(container, make_agent, models):
    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await drain(container)

    # 删除标记一提交就拒绝来信，不等删除流程的事件投递：它的会话不会再变忙
    await container.agent_lifecycle.delete(agent.id, author="user")
    for target in (NewSession(agent_id=agent.id, title="t"), session.id):
        with pytest.raises(NotFound, match="not found"):
            await container.sessions.receive(
                Inbound(origin=Origin.USER, origin_ref=None, content="hello", session=target, awaited=True)
            )
    await drain(container)

    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    assert await _failed_tasks(container) == []


async def test_an_agent_with_a_busy_session_is_not_deleted(container, make_agent, models):
    session, job, _recorder, agent = await start_job(container, make_agent, models, "sleep 30")
    with pytest.raises(Conflict, match="working"):
        await container.agent_lifecycle.delete(agent.id, author="user")
    await container.stopper(session.id)
    await drain(container)
    await wait_until_done(container, job.id)


async def test_deleting_an_agent_twice_is_a_conflict(container, make_agent):
    agent = await make_agent("alpha")
    await container.agent_lifecycle.delete(agent.id, author="user")
    with pytest.raises(Conflict, match="already being deleted"):
        await container.agent_lifecycle.delete(agent.id, author="user")
    # 正在删除的 agent 不能再改，也不能再为它写资源或 secret
    with pytest.raises(NotFound):
        await container.agents.get_active(agent.id)
    with pytest.raises(NotFound):
        await container.vault.set(agent.id, "K", "v")
    # 添加 MCP server 在连接验证之前就拒绝（这个配置一连就失败，报 InvalidInput 说明白跑了一次连接）
    failing = mcp_config(
        transport=McpTransport.STDIO, command=sys.executable, args=["-c", "raise SystemExit(1)"]
    )
    with pytest.raises(NotFound):
        await container.mcp.add(agent.id, "probe", failing, probe=True, author="user")
    await drain(container)


async def test_a_run_rerun_after_the_mark_is_stopped_and_the_agent_deleted(container, make_agent, models):
    """标记删除之后会话不再收到来信，人为重跑它的任务仍会让会话变忙：删除流程停止它（排队的运行不再执行）再删除。"""

    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await drain(container)
    await container.agent_lifecycle.delete(agent.id, author="user")
    # 模拟人为重跑：会话的 lane 上又有一个运行在排队
    async with container.db.transaction() as s:
        rerun = container.queue.enqueue(
            s,
            kind=RUN_TASK,
            lane=session_lane(session.id),
            agent_id=agent.id,
            session_id=session.id,
            payload={},
        )
    await deliver(container)

    async with container.db.transaction() as s:
        assert (await s.get(Task, rerun.id)).status == TaskStatus.DONE
    await drain(container)
    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    with pytest.raises(NotFound):
        await container.sessions.get(session.id)
    assert await _failed_tasks(container) == []


async def test_a_deleted_agents_thread_takes_no_new_delegation(container, make_agent, models):
    from muliao.errors import NotFound as Missing
    from tests.scripted import scripted
    from tests.test_mailbox import Recorder

    lead = (await make_agent("lead", model="l")).id
    reviewer = await container.agents.get((await make_agent("reviewer", model="v")).id)
    models.models["l"] = scripted(Recorder())
    parent = await start(container, lead, "go")
    await drain(container)
    async with container.db.transaction() as s:
        await container.threads.send(s, parent_id=parent.id, agent=reviewer, thread="review", prompt="x")
    await drain(container)
    await container.agent_lifecycle.delete(reviewer.id, author="user")
    await drain(container)

    with pytest.raises(Missing, match="not found"):
        async with container.db.transaction() as s:
            await container.threads.send(s, parent_id=parent.id, agent=reviewer, thread="review2", prompt="y")


async def test_notifications_of_a_deleted_agents_sessions_go_with_them(container, make_agent, models):
    agent = await make_agent("alpha")
    await start(container, agent.id, "hi")
    await drain(container)
    assert len(await container.notifications.list(10)) == 1

    await container.agent_lifecycle.delete(agent.id, author="user")
    await drain(container)

    assert await container.notifications.list(10) == []


async def test_check_ins_do_not_wake_a_stopped_session(container, make_agent, models):
    from muliao.sessions.tables import HoldStatus, Mail, SessionHold

    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    # 停止已清掉占用，但后台任务领域还没处理 SessionStopped（进程还在跑）
    await container.stopper(session.id)

    # 任务还在运行，检查命令照发；会话领域看到占用已清掉，不投检查信件
    assert await container.jobs.check() == 1
    await deliver(container)
    async with container.db.transaction() as s:
        [hold] = (await s.scalars(select(SessionHold).where(SessionHold.session_id == session.id))).all()
        mail = (
            await s.scalars(select(Mail).where(Mail.session_id == session.id, Mail.source.is_not(None)))
        ).all()
    assert hold.status == HoldStatus.CLEARED and mail == []
    await drain(container)
    await wait_until_done(container, job.id)


async def test_deleting_a_session_before_its_run_ended_is_delivered_leaves_no_notification(
    container, make_agent, models
):
    from tests.test_flow import run_only

    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await run_only(container)
    # RunEnded 还没投递就删除：通知领域按会话的同一条 lane 先处理 RunEnded、再处理 SessionDeleted
    await container.sessions.delete(session.id)
    assert await _lanes_of(container, "notifications.run_ended", "notifications.session_deleted") == {
        f"notifications:{session.id}"
    }
    await drain(container)

    assert await container.notifications.list(10) == []
    assert await _failed_tasks(container) == []


async def test_a_session_turned_busy_between_the_check_and_the_mark_is_stopped_and_deleted(
    container, make_agent, models, monkeypatch
):
    """删除前的检查与标记分属两个事务：之间来的信件排上的运行可能开出后台任务。删除流程先停止忙的会话再删除，
    不靠重试等它自己结束。"""
    session, job, _recorder, agent = await start_job(container, make_agent, models, "sleep 30")

    async def not_busy(_s, _agent_id):  # noqa: ANN202  检查时还不忙
        return False

    monkeypatch.setattr(container.agent_lifecycle, "_blockers", {"sessions": not_busy})
    await container.agent_lifecycle.delete(agent.id, author="user")
    await drain(container)
    ended = await wait_until_done(container, job.id)
    await drain(container)

    assert ended.status == JobStatus.KILLED
    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    with pytest.raises(NotFound):
        await container.sessions.get(session.id)
    assert await _failed_tasks(container) == []


async def test_a_session_of_an_agent_marked_for_deletion_cannot_be_compacted(container, make_agent, models):
    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await drain(container)
    await container.agent_lifecycle.delete(agent.id, author="user")

    with pytest.raises(NotFound):
        await container.compactor.request(session.id)


async def test_a_failed_run_ended_retried_after_the_session_was_deleted_writes_no_notification(
    container, make_agent, models, monkeypatch
):
    """失败的投递不挡同一条 lane 上之后的投递：SessionDeleted 先处理了，人工重试的 RunEnded 由处理器检查会话还在。"""
    from tests.test_flow import deliver, run_only

    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await run_only(container)

    async def broken(*_args, **_kwargs):  # noqa: ANN202
        raise RuntimeError("notification store unavailable")

    with monkeypatch.context() as m:
        m.setattr(container.notifications, "_enqueue_in", broken)
        await deliver(container)
    [failed] = [
        t for t in await _failed_tasks(container) if t.payload["receiver"] == "notifications.run_ended"
    ]

    await container.sessions.delete(session.id)
    await deliver(container)
    await container.queue.retry(failed.id)
    await drain(container)

    assert await container.notifications.list(10) == []
    assert await _failed_tasks(container) == []


async def test_deleting_an_agent_whose_run_is_executing_stops_it_and_completes(
    container, make_agent, models, monkeypatch
):
    """标记删除时有一次运行正在执行（检查与标记之间排上的）：删除流程停止它、等停止事务做完再删，一次完成。"""
    import asyncio

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    agent = await make_agent("alpha", model="m")
    started = asyncio.Event()

    async def slow(messages: list[ModelMessage], info: AgentInfo):  # noqa: ANN202
        started.set()
        await asyncio.sleep(30)
        yield "never"

    models.models["m"] = FunctionModel(stream_function=slow)
    session = await start(container, agent.id, "go")

    async def not_busy(_s, _agent_id):  # noqa: ANN202  检查时还不忙
        return False

    monkeypatch.setattr(container.agent_lifecycle, "_blockers", {"sessions": not_busy})
    worker = asyncio.create_task(container.worker.run_forever())
    try:
        await asyncio.wait_for(started.wait(), 5)
        await container.agent_lifecycle.delete(agent.id, author="user")
        from tests.test_flow import idle

        await idle(container)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
        retried = (await s.scalars(select(Task).where(Task.kind == EVENT_TASK, Task.attempts > 1))).all()
    assert retried == []
    with pytest.raises(NotFound):
        await container.sessions.get(session.id)
    assert await _failed_tasks(container) == []


async def test_agent_names_cover_agents_being_deleted_and_label_deleted_ones(container, make_agent, models):
    """名称映射（AgentNames）包括正在删除的 agent：删除流程还没清掉它的 trigger、issue 时列出它们不报错；
    删完之后留下的 id 显示为 deleted agent。"""
    agent = await make_agent("alpha")
    await start(container, agent.id, "hi")
    await drain(container)

    await container.agent_lifecycle.delete(agent.id, author="user")
    assert [a.id for a in await container.agents.list()].count(agent.id) == 0
    assert (await container.agents.names())[agent.id] == "alpha"

    await drain(container)
    assert (await container.agents.names())[agent.id] == f"(deleted agent {agent.id})"


async def test_a_deleting_agents_private_resources_can_no_longer_be_written(container, make_agent):
    """写私有资源之前 owner 必须还可用：由资源仓库在它的锁里检查（agents.active，ResourceRepo），不论写入来自 HTTP 还是工具；删除流程之后在同一把锁里删掉资源目录，所以不会留下孤儿目录。公共资源不受影响。"""
    from muliao.agents.api import McpTransport, mcp_config

    agent = await make_agent("alpha")
    await container.skills.write(agent.id, "s", "d", "b", "user")
    await container.agent_lifecycle.delete(agent.id, author="user")

    writes = [
        lambda: container.skills.write(agent.id, "t", "d", "b", "user"),
        lambda: container.skills.edit(agent.id, "s", "user", new="more"),
        lambda: container.skills.delete(agent.id, "s", "user"),
        lambda: container.memory.edit(agent.id, "", "fact", "user"),
        lambda: container.mcp.add(
            agent.id,
            "m",
            mcp_config(transport=McpTransport.HTTP, url="http://example.invalid/mcp"),
            probe=False,
            author="user",
        ),
    ]
    for write in writes:
        with pytest.raises(NotFound, match="not found"):
            await write()
    assert not (container.resources.owner_dir(agent.id) / "skills" / "t").exists()
    await container.skills.write(None, "pub", "d", "b", "user")


async def test_agent_without_resource_files_can_still_be_deleted(container, make_agent):
    agent = await make_agent("alpha")
    await container.resources.apply({container.resources.owner_dir(agent.id): None}, "lost", "user")

    await container.agent_lifecycle.delete(agent.id, author="user")
    # 删除的清理经事件完成
    await drain(container)

    assert await container.agents.list() == []
