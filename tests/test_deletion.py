"""删除经事件完成（docs/domain-design.md 4.4、5.4）：没有跨领域外键，各领域按 SessionDeleted / AgentDeleting 清理
自己的数据；先停止再删除时迟到的结局不产生失败投递。"""

import pytest
from sqlalchemy import select

from muliao.errors import Conflict, NotFound
from muliao.models import Agent, Job, JobStatus, Origin, RetiredAgent, Task, TaskStatus
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
