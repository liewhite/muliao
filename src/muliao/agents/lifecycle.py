"""删除 agent（docs/architecture.md 第 10 节）：agent 领域发起、各领域清理自己的数据的流程。这里只编排 agent 领域各部分的
接口（目录 agents.service、资源目录 agents.assets、分发 agents.access），不直接碰它们的表。

1. delete：它的会话有忙的，就拒绝（与删除会话同一条规则）；否则在一个事务里把 agent 标为正在删除（不再出现在列表与
   名称解析里，各领域的写入口按不存在处理，见 agents.active）、发布 AgentDeleting（各领域的订阅者在
   这个事务里清理自己引用它的数据，这里不列举是谁；会话领域删除它的全部会话，有忙的就整个回滚），并排上删除文件的任务。
2. 删除文件的任务（AGENT_REMOVAL_TASK，AgentRemoval）：删除工作目录与它的资源目录（`assets/<名>/`）和 access.toml 里
   它的那一节，再删除这一行；它名下分发出去的资源（agents.access.Sharing.shared）先挪到平台目录（别的 agent 还在用）。
   文件不在数据库事务里，所以单独成一个可重试的任务；重做时已不在的文件跳过。

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
from muliao.agents.kinds import as_kind
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
from muliao.platform.messaging import Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, TaskQueue, no_commit

log = logging.getLogger(__name__)


# 删除 agent 的文件与这一行（AgentRemoval）
AGENT_REMOVAL_TASK = "agent_removal"

# 归档这个 agent 会打断的工作还在进行（别的领域的只读查询，组合根注入，如它的会话忙）。删除不用它：删除时会话领域在
# 同一事务里删它的会话、按同一条规则拒绝忙的（sessions.deletion）
DeletionBlocker = Callable[[AsyncSession, str], Awaitable[bool]]


class AgentLifecycle:
    def __init__(
        self,
        db: Database,
        source: AssetSource,
        queue: TaskQueue,
        messaging: Messaging,
        blockers: Mapping[str, DeletionBlocker],
    ):
        self._db = db
        self._source = source
        self._queue = queue
        self._messaging = messaging
        self._blockers = blockers

    async def delete(self, agent_id: str, *, actor: str) -> None:
        async with self._db.transaction() as s:
            agent = await deletable_in(s, agent_id)
        # 删除最后要改 access.toml（_retire_assets）：同样的改动现在就算一遍，不合法时先拒绝，不让 agent 停在正在删除、
        # 删除文件的任务一直失败
        snapshot = await self._source.snapshot()
        await asyncio.to_thread(_plan_retire, snapshot, agent.name)
        # 标记与各领域的清理在同一个事务里，之间插不进新工作；忙不忙由会话领域在删它的会话时查（与删除会话同一条规则），
        # 忙就 Conflict、整个事务回滚
        async with self._db.transaction() as s:
            await deletable_in(s, agent_id)
            await mark_deleting_in(s, agent_id, actor)
            await self._messaging.publish_in(s, AgentDeleting(agent_id=agent_id))
            self._queue.enqueue(
                s,
                kind=AGENT_REMOVAL_TASK,
                lane=f"agents.removal:{agent_id}",
                agent_id=agent_id,
                session_id=None,
                payload={},
            )
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


class AgentRemoval:
    """删除文件的任务（AGENT_REMOVAL_TASK）：删工作目录、资源目录与 access.toml 里它的那一节，最后删除这一行。"""

    prunable = True

    def __init__(self, db: Database, agents: AgentService, source: AssetSource):
        self._db = db
        self._agents = agents
        self._source = source

    async def execute(self, task: Task) -> Commit:
        agent_id = task.agent_id
        if agent_id is None:
            raise RuntimeError(f"agent removal task {task.id} has no agent")
        async with self._db.transaction() as s:
            actor = await deletion_actor_in(s, agent_id)
        if actor is None:
            return no_commit
        # 文件不在数据库事务里：重做时它们已经不在，删除是空操作
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

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """删不掉只需可见（任务记录与日志）；agent 停在正在删除，人工重试这个任务即可接着删。"""


def _retire_assets(snapshot: Snapshot, agent_id: str) -> None:
    """删掉 agent 的资源目录与 access.toml 里它的那一节；它名下分发出去的资源（agents.access.Sharing.shared：base 收录，或别的
    agent 的规则收录）先挪到平台目录（别的 agent 还在用），私有记忆不分发、随目录删掉。目录没加载成功（agent.toml 坏了等）时它名下没有加载出的资源可挪，
    照样按数据库里的名字删目录与那一节，不留下占着这个名字的残留。"""
    directory = snapshot.own_dir(agent_id)
    assert directory is not None, "an agent being deleted is still in the database"
    if agent_id not in snapshot.agents and directory.exists():
        # 没加载出的资源无从判断别人用不用，随目录删掉：记下路径，事后好从备份里找
        log.warning(
            "directory of a deleted agent removed without being loaded", extra={"path": str(directory)}
        )
    sharing = sharing_of(snapshot)
    # 只看按名字认的资源：私有记忆只属于它的 agent，在平台没有名字，随目录删掉
    for kind, by_name in snapshot.assets.items():
        for a in by_name.values():
            if a.owner == agent_id and sharing.shared(as_kind(kind), a.name, agent_id):
                assets.move(a.path, snapshot.path_of(kind, a.name, None))
                log.info(
                    "asset moved to the platform with its agent deleted",
                    extra={"kind": kind, "asset": a.name},
                )
    _plan_retire(snapshot, directory.name).apply()
    assets.remove(directory)


def _plan_retire(snapshot: Snapshot, name: str) -> access_file.AccessEdit:
    """去掉 access.toml 里名为 name 的 agent 那一节的改动（还没写）。"""
    return access_file.plan(snapshot, lambda doc: access_file.remove_agent(doc, name))
