"""agent 目录子域对 agents 表的查询（agents 表只经目录读写）。单独成模块是因为资源目录（agents.source）也要按 id 读名称，
而目录的服务（agents.service）又依赖资源目录；这里在两者之下。

- 正在删除的按不存在处理（docs/architecture.md 第 10 节「删除 agent」）：这条规则只在 LIVE 一处，按 id 取行
  （active_row_in）、按名称找（named_row_in）、列出（listed_in）都用它；归档的另外不能运行（runnable_row_in）。行只给目录
  子域自己用，别的领域用 agents.service 的 `*_agent_in`。
- 名称：agent_names_in（含正在删除的）。

agent 只会从可用变为正在删除、再变为不存在。为某个 agent 写数据（包括它的资产与记忆）之前，在写入的同一事务里
检查它还可用（agents.service.active_agent_in）：事务串行，检查通过的写入一定落在删除标记之前，被删除流程一起删掉。
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.naming import normalize_agent_name
from muliao.agents.tables import Agent
from muliao.errors import Conflict, NotFound

# 还可用（不在删除中）的 agent
LIVE = Agent.deleting_by.is_(None)


async def active_row_in(s: AsyncSession, agent_id: str) -> Agent:
    """还可用的 agent 的行。"""
    agent = await s.scalar(select(Agent).where(Agent.id == agent_id, LIVE))
    if agent is None:
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


async def named_row_in(s: AsyncSession, name: str) -> Agent:
    """按名称找还可用的 agent（名称唯一）。没有时 NotFound，列出可用的名称。"""
    name = normalize_agent_name(name)
    agent = await s.scalar(select(Agent).where(Agent.name == name, LIVE))
    if agent is None:
        names = [a.name for a in await listed_in(s)]
        raise NotFound(f"no agent named {name!r}; available agents: {names}")
    return agent


async def listed_in(s: AsyncSession) -> list[Agent]:
    """列表里的 agent（正在删除的不列出），按名称。"""
    return list(await s.scalars(select(Agent).where(LIVE).order_by(Agent.name)))


@dataclass(frozen=True)
class AgentNames:
    """agent id → 展示名称（包括正在删除的）。别的领域里留着的 id 可能属于已删除的 agent，显示为
    「(deleted agent <id>)」，而不是报错或露出裸 id。"""

    by_id: dict[str, str]

    def __getitem__(self, agent_id: str) -> str:
        return self.by_id.get(agent_id, f"(deleted agent {agent_id})")


async def agent_names_in(s: AsyncSession) -> AgentNames:
    """在调用方的事务里取全部 agent（含正在删除的）的名称：展示用，也是读资源树时按 id 对上 agent 目录的依据（agents.source）。"""
    return AgentNames({agent_id: name for agent_id, name in await s.execute(select(Agent.id, Agent.name))})
