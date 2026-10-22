"""agent 是否还可用：正在删除的按不存在处理（docs/architecture.md 第 10 节「删除 agent」）。

agent 只会从可用变为正在删除、再变为不存在。为某个 agent 写数据之前检查它还可用：
- 数据库数据在写入的同一事务里检查（agents.service.active_agent_in）；
- 私有资源（skill、记忆、指令、MCP 配置）由资源仓库在它的锁里、写入之前检查（ActiveOwners，owner 由写入的路径推出，
  见 ResourceRepo）。删除 agent 先提交删除标记、再在同一把锁里删掉它的资源目录（AgentService.remove_files），所以
  检查通过的写入一定落在删除之前、被删除流程一起删掉，不会留下孤儿目录。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.tables import Agent
from muliao.errors import NotFound
from muliao.platform.db import Database


async def active_row_in(s: AsyncSession, agent_id: str) -> Agent:
    agent = await s.get(Agent, agent_id)
    if agent is None or agent.deleting_by is not None:
        raise NotFound(f"agent {agent_id!r} not found")
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
