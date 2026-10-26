"""条目的两条规则，只在这里写一次：

1. 能用（Grants.uses）：agent 能用的条目 = 它自己 owner 的 ∪ 它的规则（agents.permissions，base + 角色 + 自己的 allow /
   deny）放行的。deny 只作用于规则分来的，不作用于自己的条目。用户（界面）能用全部。
2. 能改（check_change_in）：用户、条目的 owner、带 admin 角色的 agent；否则 Forbidden，并说出 owner 是谁。owner 为 None
   的条目归平台（迁移前的公共条目、用户在界面上建的、内置工具）。

规则按读出时角色的定义算：运行开始时算一次工具列表（Toolbox.profile），条目在用到时（skill 索引、MCP 连接、secret
解析）按当时的定义算。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.permissions import ADMIN, Kind, Rules
from muliao.agents.revisions import USER
from muliao.agents.roles import rules_in
from muliao.agents.tables import Agent, Item
from muliao.errors import Forbidden, NotFound


@dataclass(frozen=True)
class Grants:
    """一个使用者能用什么、能改什么。agent_id 为 None 是用户：全部能用、全部能改。"""

    agent_id: str | None
    rules: Rules
    admin: bool

    def uses(self, kind: Kind, name: str, owner: str | None) -> bool:
        if self.agent_id is None:
            return True
        return owner == self.agent_id or self.rules.allows(kind, name)

    def changes(self, owner: str | None) -> bool:
        return self.agent_id is None or self.admin or owner == self.agent_id


USER_GRANTS = Grants(None, Rules(), admin=True)


async def grants_in(s: AsyncSession, actor: str) -> Grants:
    """actor（agent 的 id，或 revisions.USER 即用户）的 Grants，按角色现在的定义算。agent 包括正在删除的（它的写入另由
    active_row_in 拦下）。"""
    if actor == USER:
        return USER_GRANTS
    agent = await s.get(Agent, actor)
    if agent is None:
        raise NotFound(f"agent {actor!r} not found")
    return Grants(actor, await rules_in(s, agent.roles, agent.allow, agent.deny), ADMIN in agent.roles)


async def owner_label_in(s: AsyncSession, owner: str | None) -> str:
    """给模型与用户看的 owner：agent 名，或 platform。"""
    if owner is None:
        return "the platform"
    agent = await s.get(Agent, owner)
    return f"agent {agent.name if agent is not None else owner!r}"


async def check_change_in(s: AsyncSession, actor: str, row: Item) -> None:
    """规则 2：actor 能改这一项，否则 Forbidden。"""
    if not (await grants_in(s, actor)).changes(row.owner):
        raise Forbidden(
            f"{row.kind} {row.name!r} belongs to {await owner_label_in(s, row.owner)}; only its owner or an "
            "admin can change it"
        )
