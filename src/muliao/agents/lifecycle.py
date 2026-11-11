"""删除 agent（docs/architecture.md 第 10 节）：agent 领域发起、各领域清理自己的数据的流程。这里只编排 agent 领域各子域的
接口（目录 agents.service、资源目录 agents.assets、分发 agents.access），不直接碰它们的表。

1. delete：它的会话有忙的，就拒绝（与删除会话同一条规则）；否则把 agent 标为正在删除
   （不再出现在列表与名称解析里，各领域的写入口按不存在处理，见 agents.service.active_agent_in），同一事务发布
   AgentDeleting。
2. 各领域订阅 AgentDeleting 清理自己引用它的数据（这里不列举是谁）；其中会话领域删除它的全部会话（标记之后它的
   会话不会再收到来信），然后发布 AgentSessionsCleared。
3. 收到 AgentSessionsCleared：删除工作目录与它的资源目录（`assets/<名>/`）和 access.toml 里它的那一节，再删除这一行；
   它名下分发出去的资源（agents.access.Sharing.shared）先挪到平台目录（别的 agent 还在用）。

归档（set_archived）是可以来回切换的停用：同样只在它空闲时做（与删除同一组检查），之后给它派新工作的入口都拒绝
（agents.active.runnable_row_in），数据都留着。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents import access_file, assets
from muliao.agents.access import sharing_of
from muliao.agents.assets import Snapshot
from muliao.agents.events import AgentDeleting
from muliao.agents.kinds import StoredKind, as_kind
from muliao.agents.service import (
    AgentInfo,
    AgentService,
    active_agent_in,
    check_archivable,
    deletable_in,
    deletion_actor_in,
    mark_deleting_in,
    remove_in,
    set_archived_in,
)
from muliao.agents.source import AssetSource
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
        source: AssetSource,
        messaging: Messaging,
        blockers: Mapping[str, DeletionBlocker],
    ):
        self._db = db
        self._agents = agents
        self._source = source
        self._messaging = messaging
        self._blockers = blockers

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentSessionsCleared, "agents.sessions_cleared", self.on_sessions_cleared)

    async def delete(self, agent_id: str, *, actor: str) -> None:
        # 会话的状态是别的领域的数据：在自己的只读事务里查（docs/architecture.md 2.2）。检查与标记之间排上的工作
        # 由会话领域的订阅者先停止再删除（4.4）
        async with self._db.transaction() as s:
            agent = await deletable_in(s, agent_id)
            blocking = await self._blocking_in(s, agent_id)
        if blocking:
            log.info("agent deletion refused: busy", extra={"agent": agent_id, "blocked_by": blocking})
            raise Conflict(f"agent {agent.name!r} is working right now; stop its sessions first, then delete")
        # 删除最后要改 access.toml（_retire_assets）：它不合法时先拒绝，不让 agent 停在正在删除、一直重放
        snapshot = await self._source.snapshot()
        await asyncio.to_thread(access_file.check_valid, snapshot.access_path)
        async with self._db.transaction() as s:
            await mark_deleting_in(s, agent_id, actor)
            self._messaging.publish_in(s, AgentDeleting(agent_id=agent_id))
        log.info("agent deletion started", extra={"agent": agent_id, "actor": actor})

    async def set_archived(self, agent_id: str, archived: bool) -> AgentInfo:
        """归档（或取消归档）；已是目标状态时不做任何事。归档要求它不是系统默认 agent、且空闲（与删除同一组检查，忙时
        Conflict）。检查与标记之间排上的运行照常做完，只是之后的新工作被拒绝；已校验、还没投进会话的来信在投递时被丢弃
        （sessions.service.on_receive，与删除相同）。不查谁在归档：归档只在界面上做。"""
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
            actor = await deletion_actor_in(s, agent_id)
        if actor is None:
            return no_commit
        # 文件不在数据库事务里：重放时它们已经不在，删除是空操作
        await self._agents.remove_workspace(agent_id)
        async with self._source.writing() as snapshot:
            await asyncio.to_thread(_retire_assets, snapshot, agent_id)
        log.info("files of a deleted agent removed", extra={"agent": agent_id})

        async def commit(s: AsyncSession) -> None:
            if await deletion_actor_in(s, agent_id) is None:
                return
            await remove_in(s, agent_id)
            log.info("agent deleted", extra={"agent": agent_id, "actor": actor})

        return commit


def _retire_assets(snapshot: Snapshot, agent_id: str) -> None:
    """删掉 agent 的资源目录与 access.toml 里它的那一节；它名下分发出去的资源（agents.access.Sharing.shared：base 收录，或别的
    agent 的规则收录）先挪到平台目录（别的 agent 还在用）。目录没加载成功（agent.toml 坏了等）时它名下没有加载出的资源可挪，
    照样按数据库里的名字删目录与那一节，不留下占着这个名字的残留。"""
    directory = snapshot.own_dir(agent_id)
    assert directory is not None, "an agent being deleted is still in the database"
    if agent_id not in snapshot.agents and directory.exists():
        # 没加载出的资源无从判断别人用不用，随目录删掉：记下路径，事后好从备份里找
        log.warning(
            "directory of a deleted agent removed without being loaded", extra={"path": str(directory)}
        )
    sharing = sharing_of(snapshot)
    for kind in StoredKind:
        for a in snapshot.of_kind(kind):
            if a.owner == agent_id and sharing.shared(as_kind(kind), a.name, agent_id):
                assets.move(a.path, snapshot.path_of(kind, a.name, None))
                log.info(
                    "asset moved to the platform with its agent deleted",
                    extra={"kind": kind, "asset": a.name},
                )
    access_file.edit(snapshot.access_path, lambda doc: access_file.remove_agent(doc, directory.name))
    assets.remove(directory)
