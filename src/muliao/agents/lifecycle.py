"""删除 agent（docs/architecture.md 第 10 节）：agent 领域发起、各领域清理自己的数据的流程。这里只编排 agent 领域各子域的
接口（目录 agents.service、条目 agents.ownership、记忆 agents.memory、分发 agents.agent_rules / access），不直接碰它们的表。

1. delete：它的会话有忙的，就拒绝（与删除会话同一条规则）；否则把 agent 标为正在删除
   （不再出现在列表与名称解析里，各领域的写入口按不存在处理，见 agents.service.active_agent_in），同一事务发布
   AgentDeleting。
2. 各领域订阅 AgentDeleting 清理自己引用它的数据：会话领域删除它的全部会话（标记之后它的会话不会再收到
   来信），然后发布 AgentSessionsCleared；触发器各自删除。
3. 收到 AgentSessionsCleared：删除工作目录与它的 skill 的生成目录，再删除这一行：它名下的条目（skill、MCP server、secret）
   里分发出去的（agents.access.Sharing.shared）转给平台（owner 置空，别的 agent 还在用），其余与记忆、分发规则随行
   级联删除，同一事务里各子域为每一项记一版。

归档（set_archived）是可以来回切换的停用：同样只在它空闲时做（与删除同一组检查），之后给它派新工作的入口都拒绝
（agents.active.runnable_row_in），数据都留着。
"""

import logging
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.access import sharing_in
from muliao.agents.agent_rules import record_rules_deleted_in
from muliao.agents.events import AgentDeleting
from muliao.agents.kinds import Kind
from muliao.agents.memory import record_memory_deleted_in
from muliao.agents.ownership import retire_owned_in
from muliao.agents.service import (
    AgentInfo,
    AgentService,
    active_agent_in,
    check_archivable,
    deletable_in,
    deletion_author_in,
    mark_deleting_in,
    remove_in,
    set_archived_in,
)
from muliao.agents.skills import SkillStore
from muliao.errors import Conflict
from muliao.platform.db import Database
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
        skills: SkillStore,
        messaging: Messaging,
        blockers: Mapping[str, DeletionBlocker],
    ):
        self._db = db
        self._agents = agents
        self._skills = skills
        self._messaging = messaging
        self._blockers = blockers

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentSessionsCleared, "agents.sessions_cleared", self.on_sessions_cleared)

    async def delete(self, agent_id: str, *, author: str) -> None:
        # 会话的状态是别的领域的数据：在自己的只读事务里查（docs/architecture.md 2.2）。检查与标记之间排上的工作
        # 由会话领域的订阅者先停止再删除（4.4）
        async with self._db.transaction() as s:
            agent = await deletable_in(s, agent_id)
            blocking = await self._blocking_in(s, agent_id)
        if blocking:
            log.info("agent deletion refused: busy", extra={"agent": agent_id, "blocked_by": blocking})
            raise Conflict(f"agent {agent.name!r} is working right now; stop its sessions first, then delete")
        async with self._db.transaction() as s:
            await mark_deleting_in(s, agent_id, author)
            self._messaging.publish_in(s, AgentDeleting(agent_id=agent_id))
        log.info("agent deletion started", extra={"agent": agent_id, "author": author})

    async def set_archived(self, agent_id: str, archived: bool) -> AgentInfo:
        """归档（或取消归档）；已是目标状态时不做任何事。归档要求它不是系统默认 agent、且空闲（与删除同一组检查，忙时
        Conflict）。检查与标记之间排上的运行照常做完，只是之后的新工作被拒绝；已校验、还没投进会话的来信在投递时被丢弃
        （sessions.service.on_receive，与删除相同）。"""
        if archived:
            async with self._db.transaction() as s:
                agent = await active_agent_in(s, agent_id)
                check_archivable(agent)
                blocking = await self._blocking_in(s, agent_id)
            if blocking:
                log.info("agent archiving refused: busy", extra={"agent": agent_id, "blocked_by": blocking})
                raise Conflict(
                    f"agent {agent.name!r} is working right now; stop its sessions first, then archive"
                )
        async with self._db.transaction() as s:
            before = await active_agent_in(s, agent_id)
            info = await set_archived_in(s, agent_id, archived)
        if before.archived != archived:
            log.info("agent archived" if archived else "agent unarchived", extra={"agent": agent_id})
        return info

    async def _blocking_in(self, s: AsyncSession, agent_id: str) -> list[str]:
        """还在挡着删除的查询的名字（空表示可以删除）。"""
        return [name for name, blocked in self._blockers.items() if await blocked(s, agent_id)]

    async def on_sessions_cleared(self, event: AgentSessionsCleared, _delivery: Delivery) -> Commit:
        agent_id = event.agent_id
        async with self._db.transaction() as s:
            author = await deletion_author_in(s, agent_id)
        if author is None:
            return no_commit
        # 目录不在数据库事务里：重放时它们已经不在，删除是空操作。skill 的生成目录只是副本，先于行删除
        await self._agents.remove_workspace(agent_id)
        # 要转给平台的 skill 留着生成目录。这里与 commit 里各算一次：中间改了角色时两边可能不一致（多删或留下目录），
        # 生成目录只是副本，启动时 materialize_all 按数据库全量对齐，下次写入该 skill 也会重新生成
        async with self._db.transaction() as s:
            sharing = await sharing_in(s)
        await self._skills.remove_generated(
            [
                n
                for n in await self._skills.owned_names(agent_id)
                if not sharing.shared(Kind.SKILL, n, agent_id)
            ]
        )
        log.info("files of a deleted agent removed", extra={"agent": agent_id})

        async def commit(s: AsyncSession) -> None:
            if await deletion_author_in(s, agent_id) is None:
                return
            # 各子域先记删除的版本（版本表不随它删除，留作审计），被分发出去的条目先转给平台，再删行（级联只删还归它的）
            message = f"deleted with agent {agent_id}"
            await retire_owned_in(s, agent_id, (await sharing_in(s)).shared, author, message)
            await record_memory_deleted_in(s, agent_id, author, message)
            await record_rules_deleted_in(s, agent_id, author, message)
            await remove_in(s, agent_id)
            log.info("agent deleted", extra={"agent": agent_id, "author": author})

        return commit
