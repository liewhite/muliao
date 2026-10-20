"""删除 agent（docs/architecture.md 第 10 节）：agent 领域发起、各领域清理自己的数据的流程。

1. delete：它的会话有忙的、或它的反思在排队或运行，就拒绝（与删除会话同一条规则）；否则把 agent 标为正在删除
   （不再出现在列表与名称解析里，各领域的写入口按不存在处理，见 agents.service.active_agent_in），同一事务发布
   AgentDeleting。
2. 各领域订阅 AgentDeleting 清理自己引用它的数据：会话领域删除它的全部会话（标记之后它的会话不会再收到
   来信），然后发布 AgentSessionsCleared；触发器、vault、反思各自删除。
3. 收到 AgentSessionsCleared：它的反思还在跑就等（重试），否则删除资源目录与这一行。
"""

import logging
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.events import AgentDeleting
from muliao.agents.service import AgentService
from muliao.agents.tables import Agent
from muliao.errors import Conflict, NotFound, TransientError
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit, no_commit
from muliao.sessions.events import AgentSessionsCleared

log = logging.getLogger(__name__)


# 删除这个 agent 会打断的工作还在进行（别的领域的只读查询，组合根注入：它的会话忙、它的反思在排队或执行）
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
            if blocking := await self._blocking_in(s, event.agent_id):
                # 会话已经删光；还在进行的工作（例如执行中的反思会写资源）结束后再删
                raise TransientError(f"agent {event.agent_id} still has work running: {blocking}")
        # 资源目录不在数据库事务里：重放时它已经不在，删除是空操作
        await self._agents.remove_files(event.agent_id, author)
        log.info("resources of a deleted agent removed", extra={"agent": event.agent_id})

        async def commit(s: AsyncSession) -> None:
            agent = await s.get(Agent, event.agent_id)
            if agent is not None:
                await s.delete(agent)
                log.info("agent deleted", extra={"agent": event.agent_id, "author": author})

        return commit


async def _get_in(s: AsyncSession, agent_id: str) -> Agent:
    agent = await s.get(Agent, agent_id)
    if agent is None:
        raise NotFound(f"agent {agent_id!r} not found")
    return agent
