"""agent 是否还可用：正在删除的按不存在处理（docs/architecture.md 第 10 节「删除 agent」）；是否能运行：归档的
另外不能运行（runnable_row_in）。

agent 只会从可用变为正在删除、再变为不存在。为某个 agent 写数据之前检查它还可用：
- 数据库数据在写入的同一事务里检查（agents.service.active_agent_in）；
- 私有资源（skill、记忆、MCP 配置）由资源仓库在它的锁里、写入之前检查（ActiveOwners，owner 由写入的路径推出，
  见 ResourceRepo）。删除 agent 先提交删除标记、再在同一把锁里删掉它的资源目录（AgentService.remove_files），所以
  检查通过的写入一定落在删除之前、被删除流程一起删掉，不会留下孤儿目录。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.tables import Agent
from muliao.errors import Conflict, NotFound
from muliao.platform.db import Database


async def active_row_in(s: AsyncSession, agent_id: str) -> Agent:
    agent = await s.get(Agent, agent_id)
    if agent is None or agent.deleting_by is not None:
        raise NotFound(f"agent {agent_id!r} not found")
    return agent


class AgentArchived(Conflict):
    """agent 已归档：不接受来信、不能委派、不能被指派、它的触发器不触发，取消归档后恢复。"""


async def runnable_row_in(s: AsyncSession, agent_id: str) -> Agent:
    """能运行的 agent：还可用且没有归档。归档是可以来回切换的状态，由用户或 agent 显式切换且只在 agent 空闲时归档
    （AgentLifecycle.set_archived），所以别的领域在自己的事务里按它拒绝新工作即可；校验之后、投递之前被归档的来信在投递时
    再查一次（sessions.service.on_receive）。"""
    agent = await active_row_in(s, agent_id)
    if agent.archived_at is not None:
        raise AgentArchived(f"agent {agent.name!r} is archived; unarchive it first")
    return agent


class ActiveOwners:
    """resources.OwnerCheck 的实现：私有资源的 owner 必须是还可用的 agent。"""

    def __init__(self, db: Database):
        self._db = db

    async def __call__(self, owner: str | None) -> None:
        if owner is None:
            return
        async with self._db.transaction() as s:
            await active_row_in(s, owner)
