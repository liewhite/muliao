"""agent 目录子域给同领域别的子域的事务内查询（agents 表只经目录读写）：

- agent 的行（active_row_in / runnable_row_in，只给目录子域自己用）：正在删除的按不存在处理（docs/architecture.md 第 10 节
  「删除 agent」）；归档的另外不能运行。
- 名称：agent_names_in（含正在删除的）。

agent 只会从可用变为正在删除、再变为不存在。为某个 agent 写数据（包括它的条目与记忆）之前，在写入的同一事务里
检查它还可用（agents.service.active_agent_in）：事务串行，检查通过的写入一定落在删除标记之前，被删除流程一起删掉。
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.tables import Agent
from muliao.errors import Conflict, NotFound


async def active_row_in(s: AsyncSession, agent_id: str) -> Agent:
    """还可用的 agent 的行：只给目录子域自己（service）用，别的领域用 agents.service.active_agent_in。"""
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
    """在调用方的事务里取全部 agent（含正在删除的）的名称：展示用，也是读资源树时按 id 对上 agent 目录的依据（agents.source）。"""
    return AgentNames({agent_id: name for agent_id, name in await s.execute(select(Agent.id, Agent.name))})


@dataclass(frozen=True)
class Mirrored:
    """agents 表里从 `<名>/agent.toml` 同步来的镜像列（agents.source.AssetSource）：别的领域在自己的事务里读（展示、运行用
    的模型），旧版本回滚后也读得到最新的值。名字不在这里：名字只认数据库，目录名对不上的目录不加载（agents.assets），改名
    只走平台（AgentService.update）。model / thinking 为 None 时不同步（文件里的在当前配置下不能运行，保留上次的）。"""

    id: str
    description: str
    model: str | None
    thinking: str | None
    group: str | None


async def sync_mirror_in(s: AsyncSession, agents: list[Mirrored]) -> list[str]:
    """把镜像列改成文件里的值，返回改了的 agent 的 id。"""
    rows = {a.id: a for a in await s.scalars(select(Agent).where(Agent.id.in_([m.id for m in agents])))}
    changed = []
    for m in agents:
        row = rows.get(m.id)
        if row is None:
            continue
        before = (row.description, row.model, row.thinking, row.group)
        row.description, row.group = m.description, m.group
        if m.model is not None and m.thinking is not None:
            row.model, row.thinking = m.model, m.thinking
        if (row.description, row.model, row.thinking, row.group) != before:
            changed.append(m.id)
    await s.flush()
    return changed
