"""角色：内置的 base、admin（规则的固定部分在 agents.permissions.FIXED_RULES）与用户自建的；可编辑的规则都存在 roles 表
（内置角色的行可以不存在，即可编辑部分为空）。agent 按名称引用角色，base 每个 agent 隐含、不写进 agents.roles。

引用的一致性在写事务里保证：agent 写入角色名时在同一事务里检查角色存在，删除角色时在同一事务里检查没有 agent 引用它
（事务串行，两者不会交错）。这里是事务内的查询与校验，增删改在 agents.role_service.RoleService。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from fnmatch import fnmatchcase

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.kinds import Kind
from muliao.agents.permissions import BASE, FIXED_RULES, Role, Rules, check_patterns, kind_of
from muliao.agents.tables import Agent, Item
from muliao.agents.tables import Role as RoleRow
from muliao.errors import InvalidInput

# 内置角色在表里没有行时的描述
BUILTIN_DESCRIPTIONS: dict[str, str] = {
    BASE: "每个 agent 都有：全部非管理工具，加上这里收录的 skill、MCP server、secret",
    "admin": "全部工具（含管理 agent、角色、全局记忆），并且能改任何条目",
}


def document(role: Role) -> str:
    """角色在版本表里的文档：描述与可编辑的规则（固定的部分在代码里，不记）。"""
    return json.dumps(
        {"description": role.description, "allow": list(role.rules.allow), "deny": list(role.rules.deny)},
        ensure_ascii=False,
        indent=1,
    )


def role_of(name: str, row: RoleRow | None) -> Role:
    fixed = FIXED_RULES.get(name, Rules())
    if row is None:
        return Role(name, BUILTIN_DESCRIPTIONS[name], Rules(), fixed)
    return Role(name, row.description, Rules(tuple(row.allow), tuple(row.deny)), fixed)


async def roles_in(s: AsyncSession) -> dict[str, Role]:
    """全部角色：内置在前（按定义顺序），自建的按名称。"""
    rows = {r.name: r for r in await s.scalars(select(RoleRow).order_by(RoleRow.name))}
    builtin = {n: role_of(n, rows.get(n)) for n in FIXED_RULES}
    return {**builtin, **{n: role_of(n, r) for n, r in rows.items() if n not in FIXED_RULES}}


async def check_assignable_in(s: AsyncSession, names: Sequence[str]) -> None:
    """agent 可以选的角色：存在、不重复，不含 base（每个 agent 隐含）。"""
    roles = await roles_in(s)
    implicit = [n for n in names if n in roles and not roles[n].assignable]
    if implicit:
        raise InvalidInput(f"roles {implicit} are implicit for every agent; do not list them in roles")
    unknown = [n for n in names if n not in roles]
    if unknown or len(set(names)) != len(names):
        assignable = [r.name for r in roles.values() if r.assignable]
        raise InvalidInput(f"roles must be distinct names from {assignable}, got {list(names)}")


async def holders_in(s: AsyncSession, role: Role) -> list[Agent]:
    """带这个角色的 agent（Role.held_by：base 是全部），按名称；包括归档与正在删除的（它的行还在，删除流程完成前不让
    角色先消失）。"""
    return [a for a in await s.scalars(select(Agent).order_by(Agent.name)) if role.held_by(a.roles)]


async def check_new_items_in(s: AsyncSession, patterns: Sequence[str], before: Sequence[str]) -> None:
    """新加的 skill / mcp / secret 规则（不在 before 里的）至少匹配一个现有条目：拼错的在写入时报出。已有的规则不查——
    条目删掉再建（如 MCP 重新接入）时分发不丢，也不让改别的字段的写入失败。"""
    new = [p for p in patterns if p not in before and kind_of(p) != Kind.TOOL]
    if not new:
        return
    items = [f"{k}:{n}" for k, n in await s.execute(select(Item.kind, Item.name))]
    bad = [p for p in new if not any(fnmatchcase(i, p) for i in items)]
    if bad:
        raise InvalidInput(f"rules {bad} match no existing skill, MCP server or secret; check the names")


async def check_rules_in(s: AsyncSession, rules: Rules, before: Rules = Rules()) -> None:
    """写入一组规则之前的全部校验（agent 自己的 allow / deny 与角色的共用）。"""
    check_patterns(rules.allow)
    check_patterns(rules.deny)
    await check_new_items_in(s, rules.allow, before.allow)
    await check_new_items_in(s, rules.deny, before.deny)


async def rules_in(s: AsyncSession, roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]) -> Rules:
    """agent 的最终规则：base + 它的角色 + 它自己的 allow / deny。读到不存在的角色说明数据与代码不一致（写入时已校验、
    被引用的角色不能删；内置角色从代码里去掉时启动检查会报出）。"""
    known = await roles_in(s)
    missing = [r for r in roles if r not in known]
    if missing:
        raise RuntimeError(f"agent references unknown roles {missing}")
    return Rules.union([*(known[r].effective for r in (BASE, *roles)), Rules(tuple(allow), tuple(deny))])
