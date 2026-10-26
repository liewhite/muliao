"""删除 agent（docs/architecture.md 第 10 节）：agent 领域发起、各领域清理自己的数据的流程。

1. delete：它的会话有忙的，就拒绝（与删除会话同一条规则）；否则把 agent 标为正在删除
   （不再出现在列表与名称解析里，各领域的写入口按不存在处理，见 agents.service.active_agent_in），同一事务发布
   AgentDeleting。
2. 各领域订阅 AgentDeleting 清理自己引用它的数据：会话领域删除它的全部会话（标记之后它的会话不会再收到
   来信），然后发布 AgentSessionsCleared；触发器、vault 各自删除。
3. 收到 AgentSessionsCleared：删除资源目录与这一行。

归档（set_archived）是可以来回切换的停用：同样只在它空闲时做（与删除同一组检查），之后给它派新工作的入口都拒绝
（agents.active.runnable_row_in），数据都留着。
"""

import logging
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.active import active_row_in
from muliao.agents.events import AgentDeleting
from muliao.agents.service import AgentInfo, AgentService, info_of
from muliao.agents.tables import Agent
from muliao.errors import Conflict, NotFound
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit, no_commit
from muliao.sessions.events import AgentSessionsCleared

log = logging.getLogger(__name__)


# 删除（或归档）这个 agent 会打断的工作还在进行（别的领域的只读查询，组合根注入，如它的会话忙）
DeletionBlocker = Callable[[AsyncSession, str], Awaitable[bool]]


class AgentLifecycle:
    def __init__(
        self,
        db: Database,
        agents: AgentService,
        messaging: Messaging,
        blockers: Mapping[str, DeletionBlocker],
    ):
        self._db = db
        self._agents = agents
        self._messaging = messaging
        self._blockers = blockers

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentSessionsCleared, "agents.sessions_cleared", self.on_sessions_cleared)

    async def delete(self, agent_id: str, *, author: str) -> None:
        # 会话的状态是别的领域的数据：在自己的只读事务里查（docs/architecture.md 2.2）。检查与标记之间排上的工作
        # 由会话领域的订阅者先停止再删除（4.4）
        async with self._db.transaction() as s:
            agent = await _get_in(s, agent_id)
            if agent.deleting_by is not None:
                raise Conflict(f"agent {agent.name!r} is already being deleted")
            blocking = await self._blocking_in(s, agent_id)
        if blocking:
            log.info("agent deletion refused: busy", extra={"agent": agent_id, "blocked_by": blocking})
            raise Conflict(f"agent {agent.name!r} is working right now; stop its sessions first, then delete")
        async with self._db.transaction() as s:
            agent = await _get_in(s, agent_id)
            if agent.deleting_by is not None:
                raise Conflict(f"agent {agent.name!r} is already being deleted")
            agent.deleting_by = author
            self._messaging.publish_in(s, AgentDeleting(agent_id=agent_id))
        log.info("agent deletion started", extra={"agent": agent_id, "author": author})

    async def set_archived(self, agent_id: str, archived: bool) -> AgentInfo:
        """归档（或取消归档）；已是目标状态时不做任何事。归档要求它不是系统默认 agent、且空闲（与删除同一组检查，忙时
        Conflict）。检查与标记之间排上的运行照常做完，只是之后的新工作被拒绝；已校验、还没投进会话的来信在投递时被丢弃
        （sessions.service.on_receive，与删除相同）。"""
        if archived:
            async with self._db.transaction() as s:
                agent = _check_archivable(await active_row_in(s, agent_id))
                blocking = await self._blocking_in(s, agent_id)
            if blocking:
                log.info("agent archiving refused: busy", extra={"agent": agent_id, "blocked_by": blocking})
                raise Conflict(
                    f"agent {agent.name!r} is working right now; stop its sessions first, then archive"
                )
        async with self._db.transaction() as s:
            agent = await active_row_in(s, agent_id)
            if (agent.archived_at is not None) == archived:
                return info_of(agent)
            if archived:
                _check_archivable(agent)
            agent.archived_at = utcnow() if archived else None
            await s.flush()
            info = info_of(agent)
        log.info("agent archived" if archived else "agent unarchived", extra={"agent": agent_id})
        return info

    async def _blocking_in(self, s: AsyncSession, agent_id: str) -> list[str]:
        """还在挡着删除的查询的名字（空表示可以删除）。"""
        return [name for name, blocked in self._blockers.items() if await blocked(s, agent_id)]

    async def on_sessions_cleared(self, event: AgentSessionsCleared, _delivery: Delivery) -> Commit:
        async with self._db.transaction() as s:
            agent = await s.get(Agent, event.agent_id)
            if agent is None:
                return no_commit
            if agent.deleting_by is None:
                raise RuntimeError(
                    f"agent {event.agent_id} had its sessions cleared but is not being deleted"
                )
            author = agent.deleting_by
        # 资源目录不在数据库事务里：重放时它已经不在，删除是空操作
        await self._agents.remove_files(event.agent_id, author)
        log.info("resources of a deleted agent removed", extra={"agent": event.agent_id})

        async def commit(s: AsyncSession) -> None:
            agent = await s.get(Agent, event.agent_id)
            if agent is not None:
                await s.delete(agent)
                log.info("agent deleted", extra={"agent": event.agent_id, "author": author})

        return commit


def _check_archivable(agent: Agent) -> Agent:
    if agent.is_default:
        raise Conflict(f"agent {agent.name!r} is the default agent; unset it as default first, then archive")
    return agent


async def _get_in(s: AsyncSession, agent_id: str) -> Agent:
    agent = await s.get(Agent, agent_id)
    if agent is None:
        raise NotFound(f"agent {agent_id!r} not found")
    return agent
