"""删除（docs/architecture.md 第 10 节与第 7 节「thread 与结果路由」）：没有跨领域外键，各领域订阅 SessionDeleted / AgentDeleting，
在删除的事务里清理自己的数据；先停止再删除时迟到的结局不产生失败任务。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

import muliao.agents.service
from muliao.agents.tables import Agent
from muliao.errors import Conflict, NotFound
from muliao.platform.tables import Task, TaskStatus
from muliao.sessions.api import NewChat, UserMessage
from muliao.workspace.tables import Job, JobStatus
from tests.conftest import owned
from tests.test_flow import drain, side_tasks, start
from tests.test_jobs import start_job, wait_until_done


async def _failed_tasks(container) -> list[Task]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Task).where(Task.status == TaskStatus.FAILED))).all())


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
    await container.agent_lifecycle.delete(agent.id, actor="user")
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

    await container.agent_lifecycle.delete(agent.id, actor="user")
    await drain(container)

    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    assert await _failed_tasks(container) == []
    assert "workspace of a deleted agent could not be fully removed" in caplog.text
    # 残留由启动检查列进日志
    caplog.clear()
    await container.agents.report_leftovers()
    [leftover] = [r for r in caplog.records if r.getMessage() == "workspaces of deleted agents remain"]
    assert leftover.agent_ids == [agent.id]


async def test_the_startup_check_does_not_report_an_agent_still_being_deleted(container, make_agent, caplog):
    agent = await make_agent("alpha")
    container.agents.workspace(agent.id).mkdir(parents=True, exist_ok=True)
    # 只标记、不投递：它的目录还在是正常的，删除流程会删
    await container.agent_lifecycle.delete(agent.id, actor="user")

    await container.agents.report_leftovers()

    assert "deleted agents remain" not in caplog.text


async def test_deleting_an_agent_removes_its_sessions_triggers_and_secrets(container, make_agent):
    agent = await make_agent("alpha")
    await container.vault.set("TOKEN", "x", "", actor="user", owner=agent.id)
    await container.triggers.create(
        name="nightly", agent_id=agent.id, kind="schedule", prompt="p", config={"cron": "0 3 * * *"}
    )
    session = await start(container, agent.id, "hi")
    await drain(container)

    await container.agent_lifecycle.delete(agent.id, actor="user")
    # 删除进行中：不再出现在列表与名称解析里
    assert agent.id not in [a.id for a in await container.agents.list()]
    with pytest.raises(NotFound):
        await container.agents.find_by_name("alpha")
    await drain(container)

    assert await container.triggers.list() == []
    assert await owned(container, "secret", agent.id) == []
    with pytest.raises(NotFound):
        await container.sessions.get(session.id)
    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    assert await _failed_tasks(container) == []


async def test_an_agent_marked_for_deletion_receives_no_new_mail(container, make_agent, models):
    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await drain(container)

    # 删除标记一提交就拒绝来信（删除文件的任务还没跑）：它的会话已删除，也不会新建
    await container.agent_lifecycle.delete(agent.id, actor="user")
    for target in (NewChat(agent.id), session.id):
        with pytest.raises(NotFound, match="not found"):
            await container.sessions.receive(UserMessage(content="hello", session=target))
    await drain(container)

    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    assert await _failed_tasks(container) == []


async def test_an_agent_with_a_busy_session_is_not_deleted(container, make_agent, models):
    session, job, _recorder, agent = await start_job(container, make_agent, models, "sleep 30")
    with pytest.raises(Conflict, match="working"):
        await container.agent_lifecycle.delete(agent.id, actor="user")
    await container.stopper(session.id)
    await drain(container)
    await wait_until_done(container, job.id)


async def test_deleting_an_agent_twice_is_a_conflict(container, make_agent):
    agent = await make_agent("alpha")
    await container.agent_lifecycle.delete(agent.id, actor="user")
    with pytest.raises(Conflict, match="already being deleted"):
        await container.agent_lifecycle.delete(agent.id, actor="user")
    # 正在删除的 agent 不能再改（它的资源目录随删除流程收尾时删掉）
    with pytest.raises(NotFound):
        await container.agents.get_active(agent.id)
    await drain(container)


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
        await container.threads.send(
            s, parent_id=parent.id, agent=reviewer, thread="review", prompt="x", call_id="c1"
        )
    await drain(container)
    await container.agent_lifecycle.delete(reviewer.id, actor="user")
    await drain(container)

    with pytest.raises(Missing, match="not found"):
        async with container.db.transaction() as s:
            await container.threads.send(
                s, parent_id=parent.id, agent=reviewer, thread="review2", prompt="y", call_id="c2"
            )


async def test_notifications_of_a_deleted_agents_sessions_go_with_them(container, make_agent, models):
    agent = await make_agent("alpha")
    await start(container, agent.id, "hi")
    await drain(container)
    assert len(await container.notifications.list(10)) == 1

    await container.agent_lifecycle.delete(agent.id, actor="user")
    await drain(container)

    assert await container.notifications.list(10) == []


async def test_check_ins_do_not_wake_a_stopped_session(container, make_agent, models, monkeypatch):
    from muliao.sessions.tables import HoldStatus, Mail, SessionHold

    session, job, _recorder, agent = await start_job(container, make_agent, models, "sleep 30")
    # 停止已清掉占用，但进程还没被结束（停止提交之后才杀，监督者记下结局之前有一段窗口）
    with monkeypatch.context() as m:
        m.setattr(container.jobs, "_kill_cleared", lambda _event: None)
        await container.stopper(session.id)

    # 任务还在运行，检查命令照发；会话领域看到占用已清掉，不投检查信件
    assert await container.jobs.check() == 1
    await side_tasks(container)
    async with container.db.transaction() as s:
        [hold] = (await s.scalars(select(SessionHold).where(SessionHold.session_id == session.id))).all()
        mail = (
            await s.scalars(select(Mail).where(Mail.session_id == session.id, Mail.source.is_not(None)))
        ).all()
    assert hold.status == HoldStatus.CLEARED and mail == []
    await container.jobs.kill(job.id, agent.id)
    await drain(container)
    await wait_until_done(container, job.id)


async def test_a_busy_session_found_while_deleting_rolls_the_whole_deletion_back(
    container, make_agent, models, monkeypatch
):
    """会话领域在删除 agent 的事务里删它的会话，发现忙的就报冲突：标记与别的领域的清理一起回滚，agent 原样留着。"""
    session, job, _recorder, agent = await start_job(container, make_agent, models, "sleep 30")
    await container.triggers.create(
        name="t", agent_id=agent.id, kind="schedule", prompt="p", config={"cron": "0 9 * * *"}
    )

    async def not_busy(_s, _agent_id):  # noqa: ANN202  前置检查放过去
        return False

    monkeypatch.setattr(container.agent_lifecycle, "_blockers", {"sessions": not_busy})
    with pytest.raises(Conflict, match="busy"):
        await container.agent_lifecycle.delete(agent.id, actor="user")

    async with container.db.transaction() as s:
        assert (await s.get(Agent, agent.id)).deleting_by is None
    assert [x.name for x in await container.triggers.list()] == ["t"]
    await container.sessions.get(session.id)
    await container.stopper(session.id)
    await drain(container)
    await wait_until_done(container, job.id)


async def test_a_session_of_an_agent_marked_for_deletion_cannot_be_compacted(container, make_agent, models):
    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await drain(container)
    await container.agent_lifecycle.delete(agent.id, actor="user")

    with pytest.raises(NotFound):
        await container.compactor.request(session.id)


async def test_agent_names_cover_agents_being_deleted_and_label_deleted_ones(container, make_agent, models):
    """名称映射（AgentNames）包括正在删除的 agent：删除流程还没清掉它的 trigger、issue 时列出它们不报错；
    删完之后留下的 id 显示为 deleted agent。"""
    agent = await make_agent("alpha")
    await start(container, agent.id, "hi")
    await drain(container)

    await container.agent_lifecycle.delete(agent.id, actor="user")
    assert [a.id for a in await container.agents.list()].count(agent.id) == 0
    assert (await container.agents.names())[agent.id] == "alpha"

    await drain(container)
    assert (await container.agents.names())[agent.id] == f"(deleted agent {agent.id})"


async def test_agent_whose_private_memory_was_deleted_can_still_be_deleted(container, make_agent):
    agent = await make_agent("alpha")
    (container.settings.assets_dir / "alpha" / "memory.md").unlink()
    assert (await container.assets.snapshot()).private_memory(agent.id) is None

    await container.agent_lifecycle.delete(agent.id, actor="user")
    # 删除的清理经事件完成
    await drain(container)

    assert await container.agents.list() == []


async def test_a_run_that_starts_after_the_mark_does_not_fail_on_its_memory(container, make_agent, models):
    """组装指令时读记忆包括正在删除的 agent（资源目录在删除文件的任务里才删）：标记之后还在跑的运行（如人为重跑）不因读不到
    记忆而失败（不产生失败通知、issue 的 run_failed 动态）。"""
    from tests.test_flow import run_only

    agent = await make_agent("alpha")
    await container.memory.put_private(
        agent.id, "- fact", version=(await container.memory.read_private(agent.id)).version
    )
    await start(container, agent.id, "hi")
    async with container.db.transaction() as s:
        (await s.get(Agent, agent.id)).deleting_by = "user"

    await run_only(container)

    assert await _failed_tasks(container) == []
    assert "- fact" in [m.content for m in await container.memory.for_run(agent.id)]
