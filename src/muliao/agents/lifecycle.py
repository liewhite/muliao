"""删除 agent（docs/domain-design.md 4.4）：agent 领域发起、各领域清理自己的数据的流程。

1. delete：它的会话有忙的就拒绝（与删除会话同一条规则）；否则把 agent 标为正在删除（不再出现在列表与名称解析里），
   同一事务发布 AgentDeleting。
2. 各领域订阅 AgentDeleting 清理自己引用它的数据：会话领域记下它已退役并删除它的全部会话，然后发布
   AgentSessionsCleared；触发器、vault、反思各自删除。
3. 收到 AgentSessionsCleared：它的反思还在跑就等（重试），否则删除资源目录与这一行。
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.events import AgentDeleting
from muliao.agents.service import AgentService
from muliao.errors import Conflict, TransientError
from muliao.models import Agent
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit, TaskQueue, no_commit, reflection_lane
from muliao.sessions.conversation import agent_busy_in
from muliao.sessions.events import AgentSessionsCleared

log = logging.getLogger(__name__)


class AgentLifecycle:
    def __init__(self, db: Database, agents: AgentService, queue: TaskQueue, messaging: Messaging):
        self._db = db
        self._agents = agents
        self._queue = queue
        self._messaging = messaging

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentSessionsCleared, "agents.sessions_cleared", self.on_sessions_cleared)

    async def delete(self, agent_id: str, *, author: str) -> None:
        # 会话的状态是别的领域的数据：在自己的只读事务里查（docs/domain-design.md 1.1），检查与标记之间的极小窗口
        # 由会话领域的订阅者重试收敛
        async with self._db.transaction() as s:
            agent = await self._agents.get_in(s, agent_id)
            if agent.deleting_by is not None:
                raise Conflict(f"agent {agent.name!r} is already being deleted")
            busy = await agent_busy_in(s, self._queue, agent_id)
        if busy:
            log.info("agent deletion refused: busy", extra={"agent": agent_id})
            raise Conflict(f"agent {agent.name!r} is working right now; stop its sessions first, then delete")
        async with self._db.transaction() as s:
            agent = await self._agents.get_in(s, agent_id)
            agent.deleting_by = author
            self._messaging.publish_in(s, AgentDeleting(agent_id=agent_id))
        log.info("agent deletion started", extra={"agent": agent_id, "author": author})

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
            if await self._queue.lane_busy(s, reflection_lane(event.agent_id)):
                # 正在执行的反思会写它的资源：等它结束再删
                raise TransientError(f"agent {event.agent_id} still has a reflection running")
        # 资源目录不在数据库事务里：重放时它已经不在，删除是空操作
        await self._agents.remove_files(event.agent_id, author)

        async def commit(s: AsyncSession) -> None:
            agent = await s.get(Agent, event.agent_id)
            if agent is not None:
                await s.delete(agent)
                log.info("agent deleted", extra={"agent": event.agent_id, "author": author})

        return commit
