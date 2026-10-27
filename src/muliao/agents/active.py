"""agent 目录子域给同领域别的子域的事务内查询（agents 表只经目录读写）：

- agent 是否还可用：正在删除的按不存在处理（docs/architecture.md 第 10 节「删除 agent」）；是否能运行：归档的
  另外不能运行（runnable_row_in）。
- 名称：agent_names_in（含正在删除的）、agent_name_in。
- 正在删除的 agent：deleting_ids（子查询；它们的条目按不存在处理）。

agent 只会从可用变为正在删除、再变为不存在。为某个 agent 写数据（包括它的条目与记忆）之前，在写入的同一事务里
检查它还可用（agents.service.active_agent_in）：事务串行，检查通过的写入一定落在删除标记之前，被删除流程一起删掉。
"""

from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.tables import Agent
from muliao.errors import Conflict, NotFound


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


@dataclass(frozen=True)
class AgentNames:
    """agent id → 展示名称（包括正在删除的）。别的领域里留着的 id 可能属于已删除的 agent，显示为
    「(deleted agent <id>)」，而不是报错或露出裸 id。"""

    by_id: dict[str, str]

    def __getitem__(self, agent_id: str) -> str:
        return self.by_id.get(agent_id, f"(deleted agent {agent_id})")


async def agent_names_in(s: AsyncSession) -> AgentNames:
    """在调用方的事务里取全部 agent 的名称（AgentNames）。"""
    return AgentNames({agent_id: name for agent_id, name in await s.execute(select(Agent.id, Agent.name))})


async def agent_name_in(s: AsyncSession, agent_id: str) -> str | None:
    """agent 的名称（包括正在删除的）；不存在时 None。"""
    return await s.scalar(select(Agent.name).where(Agent.id == agent_id))


def deleting_ids() -> Select[tuple[str]]:
    """正在删除的 agent 的 id（子查询：别的子域在自己的查询里排除它们名下的数据）。"""
    return select(Agent.id).where(Agent.deleting_by.is_not(None))
