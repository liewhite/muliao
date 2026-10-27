"""agent 的分发规则（表 agent_rules，分发子域）：角色（不含隐含的 base）与它自己的 allow / deny。每次修改在同一个写事务里
记一版（revisions，subject agent，name 是 agent 的 id，文档是这三项）；建与删随 agent 的行（目录子域在同一事务里调这里的
`*_in`）。最终规则（base + 角色 + allow / deny）按读出时角色的定义算。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.active import agent_names_in
from muliao.agents.kinds import Subject
from muliao.agents.permissions import Role, Rules
from muliao.agents.revisions import record_in
from muliao.agents.roles import check_assignable_in, check_rules_in, rules_of
from muliao.agents.tables import AgentRulesRow
from muliao.errors import NotFound


@dataclass(frozen=True)
class AgentRules:
    """agent 的分发规则：角色（不含每个 agent 隐含的 base）与它自己的 allow / deny（`kind:glob`）。"""

    roles: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @classmethod
    def of(cls, roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]) -> AgentRules:
        return cls(tuple(roles), tuple(allow), tuple(deny))

    @property
    def own(self) -> Rules:
        """它自己的 allow / deny（不含角色）。"""
        return Rules(self.allow, self.deny)

    def effective(self, known: Mapping[str, Role]) -> Rules:
        """最终规则：base + 角色 + 自己的 allow / deny；known 是 roles.roles_in 读出的全部角色。"""
        return rules_of(known, self.roles, self.allow, self.deny)

    def document(self) -> str:
        """在版本表里的文档。"""
        return json.dumps(
            {"roles": list(self.roles), "allow": list(self.allow), "deny": list(self.deny)},
            ensure_ascii=False,
            indent=1,
        )


def _of(row: AgentRulesRow) -> AgentRules:
    return AgentRules.of(row.roles, row.allow, row.deny)


async def _row_in(s: AsyncSession, agent_id: str) -> AgentRulesRow:
    row = await s.get(AgentRulesRow, agent_id)
    if row is None:
        raise NotFound(f"agent {agent_id!r} not found")
    return row


async def agent_rules_in(s: AsyncSession, agent_id: str) -> AgentRules:
    """agent 的规则（包括正在删除的；它的写入另由 check_active_in 拦下）。没有这个 agent 时 NotFound。"""
    return _of(await _row_in(s, agent_id))


async def all_agent_rules_in(s: AsyncSession) -> dict[str, AgentRules]:
    """全部 agent 的规则（包括正在删除的），按 id。"""
    rows = await s.scalars(select(AgentRulesRow).order_by(AgentRulesRow.agent_id))
    return {r.agent_id: _of(r) for r in rows}


async def check_agent_rules_in(s: AsyncSession, rules: AgentRules, before: AgentRules = AgentRules()) -> None:
    """写入（或预览）之前的校验：角色可选，规则形式合法，新加的条目规则匹配现有条目（before 里已有的不查）。"""
    await check_assignable_in(s, rules.roles)
    await check_rules_in(s, rules.own, before.own)


async def create_agent_rules_in(
    s: AsyncSession, agent_id: str, rules: AgentRules, actor: str, message: str
) -> None:
    """新建 agent 的规则，在插入它的行的同一事务里（目录子域 AgentService.create）。"""
    await check_agent_rules_in(s, rules)
    s.add(
        AgentRulesRow(
            agent_id=agent_id, roles=list(rules.roles), allow=list(rules.allow), deny=list(rules.deny)
        )
    )
    record_in(s, Subject.AGENT, agent_id, None, rules.document(), actor, message)


async def update_agent_rules_in(
    s: AsyncSession, agent_id: str, rules: AgentRules, actor: str, message: str
) -> None:
    """整体替换 agent 的规则（AgentService.update 与它别的字段同一事务），记一版（没变时 record_in 不记）。"""
    row = await _row_in(s, agent_id)
    before = _of(row)
    await check_agent_rules_in(s, rules, before)
    record_in(s, Subject.AGENT, agent_id, before.document(), rules.document(), actor, message)
    # JSON 列只在整体赋值时被追踪
    row.roles, row.allow, row.deny = list(rules.roles), list(rules.allow), list(rules.deny)


async def record_rules_deleted_in(s: AsyncSession, agent_id: str, actor: str, message: str) -> None:
    """agent 删除时为它的规则记一版删除（行随 agent 的行级联删除；版本表留作审计）。"""
    record_in(
        s, Subject.AGENT, agent_id, (await agent_rules_in(s, agent_id)).document(), None, actor, message
    )


async def holders_in(s: AsyncSession, role: Role) -> list[str]:
    """带这个角色的 agent 的 id（Role.held_by：base 是全部），按名称；包括归档与正在删除的（它的行还在，删除流程完成前
    不让角色先消失）。"""
    names = await agent_names_in(s)
    holders = [a for a, r in (await all_agent_rules_in(s)).items() if role.held_by(r.roles)]
    return sorted(holders, key=lambda a: names[a])
