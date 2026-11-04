"""角色（分发子域）：内置的 base、admin（规则的固定部分在 agents.permissions.FIXED_RULES）与用户自建的；可编辑的规则都存在
roles 表（内置角色的行可以不存在，即可编辑部分为空）。agent 按名称引用角色（agents.agent_rules），base 每个 agent 隐含、不写进
它的 roles。

引用的一致性在写事务里保证：agent 写入角色名时在同一事务里检查角色存在，删除角色时在同一事务里检查没有 agent 引用它
（事务串行，两者不会交错）。这里是事务内的查询与校验，增删改在 agents.distribution.Distribution。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.items import patterns_in
from muliao.agents.kinds import Kind
from muliao.agents.permissions import BASE, FIXED_RULES, Action, Role, Rules, check_patterns, kind_of
from muliao.agents.tables import Role as RoleRow
from muliao.errors import InvalidInput

# 内置角色在表里没有行时的描述
BUILTIN_DESCRIPTIONS: dict[str, str] = {
    BASE: "每个 agent 都有：全部非管理工具与全局记忆，加上这里收录的 skill、MCP server、secret、记忆",
    "admin": "全部工具（含管理 agent、角色、记忆的增删），并且能改任何 AI 资产",
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


async def check_new_items_in(s: AsyncSession, patterns: Sequence[str], before: Sequence[str]) -> None:
    """新加的针对某种存在 items 里的资产的规则（不在 before 里的）至少匹配一个现有资产：拼错的在写入时报出。已有的
    规则不查——资产删掉再建（如 MCP 重新接入）时分发不丢，也不让改别的字段的写入失败。不限种类的（`write:*`）不查。"""
    new = [p for p in patterns if p not in before and kind_of(p) not in (Kind.TOOL, None)]
    if not new:
        return
    items = [f"{a.value}:{i}" for i in await patterns_in(s) for a in Action]
    bad = [p for p in new if not any(fnmatchcase(i, p) for i in items)]
    if bad:
        raise InvalidInput(
            f"rules {bad} match no existing skill, MCP server, secret or memory; check the names"
        )


async def check_rules_in(s: AsyncSession, rules: Rules, before: Rules = Rules()) -> None:
    """写入一组规则之前的全部校验（agent 自己的 allow / deny 与角色的共用）。"""
    check_patterns(rules.allow)
    check_patterns(rules.deny)
    await check_new_items_in(s, rules.allow, before.allow)
    await check_new_items_in(s, rules.deny, before.deny)


def sources_of(
    known: Mapping[str, Role], roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]
) -> list[tuple[str | None, Rules]]:
    """agent 规则的各个来源：(角色名, 它的规则)——base 在前、再按它的 roles——最后是 (None, 它自己的 allow / deny)。
    最终规则就是它们的并集（rules_of）；界面按来源解释（access.Provenance）用同一份。读到不存在的角色说明数据与代码不一致
    （写入时已校验、被引用的角色不能删；内置角色从代码里去掉时启动检查会报出）。"""
    missing = [r for r in roles if r not in known]
    if missing:
        raise RuntimeError(f"agent references unknown roles {missing}")
    return [*((r, known[r].effective) for r in (BASE, *roles)), (None, Rules(tuple(allow), tuple(deny)))]


def rules_of(
    known: Mapping[str, Role], roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]
) -> Rules:
    """agent 的最终规则（AgentRules.effective）：各来源（sources_of）的并集；known 是 roles_in 读出的全部角色
    （一次读出、算多个 agent 时共用）。"""
    return Rules.union(r for _, r in sources_of(known, roles, allow, deny))
