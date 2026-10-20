"""删除经事件完成（docs/domain-design.md 4.4、5.4）：没有跨领域外键，各领域按 SessionDeleted / AgentDeleting 清理
自己的数据；先停止再删除时迟到的结局不产生失败投递。"""

import pytest
from sqlalchemy import select

from muliao.errors import Conflict, NotFound
from muliao.models import Agent, Job, JobStatus, Origin, RetiredAgent, Task, TaskKind, TaskStatus
from muliao.sessions.conversation import Inbound, NewSession
from tests.test_flow import drain, start
from tests.test_jobs import start_job, wait_until_done


async def _failed_tasks(container) -> list[Task]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Task).where(Task.status == TaskStatus.FAILED))).all())


async def test_stopping_then_deleting_right_away_leaves_no_failed_delivery(container, make_agent, models):
    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")

    await container.consumer.stop_session(session.id)
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
        assert await s.get(RetiredAgent, agent.id) is not None
    assert await _failed_tasks(container) == []


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
    assert await container.vault.list_all() == []
    with pytest.raises(NotFound):
        await container.sessions.get(session.id)
    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    assert await _failed_tasks(container) == []


async def test_a_retired_agent_receives_no_new_mail(container, make_agent):
    agent = await make_agent("alpha")
    await container.agent_lifecycle.delete(agent.id, author="user")
    await drain(container)

    with pytest.raises(NotFound, match="deleted"):
        await container.sessions.receive(
            Inbound(
                origin=Origin.USER,
                origin_ref=None,
                content="hello",
                session=NewSession(agent_id=agent.id, title="t"),
                awaited=True,
            )
        )


async def test_an_agent_with_a_busy_session_is_not_deleted(container, make_agent, models):
    session, job, _recorder, agent = await start_job(container, make_agent, models, "sleep 30")
    with pytest.raises(Conflict, match="working"):
        await container.agent_lifecycle.delete(agent.id, author="user")
    await container.consumer.stop_session(session.id)
    await drain(container)
    await wait_until_done(container, job.id)


async def test_an_agent_with_a_queued_reflection_is_not_deleted(container, make_agent, models):
    from muliao.platform.tasks import reflection_lane

    agent = await make_agent("alpha")
    async with container.db.transaction() as s:
        container.queue.enqueue(
            s,
            kind=TaskKind.REFLECTION,
            lane=reflection_lane(agent.id),
            agent_id=agent.id,
            session_id=None,
            payload={"from_message_id": 0, "to_message_id": 0},
        )
    with pytest.raises(Conflict, match="working"):
        await container.agent_lifecycle.delete(agent.id, author="user")


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
    await drain(container)


async def test_a_session_that_turns_busy_meanwhile_is_cleared_later_and_the_agent_stays_retired(
    container, make_agent, models
):
    """检查与标记之间会话变忙：清理会话的命令按忙重试；退役已经单独生效，重试期间新的来信一律拒绝。重试用完时投递
    记为失败（可见），人工重试即可继续。"""
    from muliao.models import TaskKind
    from tests.test_flow import deliver, reply

    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await drain(container)
    await container.agent_lifecycle.delete(agent.id, author="user")
    # 删除开始之后、退役生效之前，会话又收到一封信，排上了一次运行
    await reply(container, session.id, "one more thing")

    await deliver(container)

    [clear] = [t for t in await _failed_tasks(container) if t.kind == TaskKind.EVENT]
    assert "busy session" in (clear.error or "")
    with pytest.raises(NotFound, match="deleted"):
        await reply(container, session.id, "and another")
    await drain(container)
    await container.queue.retry(clear.id)
    await drain(container)
    async with container.db.transaction() as s:
        assert await s.get(Agent, agent.id) is None
    with pytest.raises(NotFound):
        await container.sessions.get(session.id)


async def test_a_retired_agents_thread_takes_no_new_delegation(container, make_agent, models):
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

    with pytest.raises(Missing, match="deleted"):
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
    from muliao.models import HoldStatus, SessionHold

    session, job, _recorder, _agent = await start_job(container, make_agent, models, "sleep 30")
    # 停止已清掉占用，但后台任务领域还没处理 SessionStopped（进程还在跑）
    await container.consumer.stop_session(session.id)

    assert await container.jobs.check() == 0
    async with container.db.transaction() as s:
        [hold] = (await s.scalars(select(SessionHold).where(SessionHold.session_id == session.id))).all()
    assert hold.status == HoldStatus.CLEARED
    await drain(container)
    await wait_until_done(container, job.id)
