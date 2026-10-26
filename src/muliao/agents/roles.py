"""角色：内置的 base、admin（规则的固定部分在 agents.permissions.FIXED_RULES）与用户自建的；可编辑的规则都存在 roles 表
（内置角色的行可以不存在，即可编辑部分为空）。agent 按名称引用角色，base 每个 agent 隐含、不写进 agents.roles。

引用的一致性在写事务里保证：agent 写入角色名时在同一事务里检查角色存在，删除角色时在同一事务里检查没有 agent 引用它
（事务串行，两者不会交错）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.naming import validate_slug
from muliao.agents.permissions import BASE, FIXED_RULES, Kind, Role, Rules, check_patterns, kind_of
from muliao.agents.tables import Agent, Item
from muliao.agents.tables import Role as RoleRow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database

# 内置角色在表里没有行时的描述
BUILTIN_DESCRIPTIONS: dict[str, str] = {
    BASE: "每个 agent 都有：全部非管理工具，加上这里收录的 skill、MCP server、secret",
    "admin": "全部工具（含管理 agent、角色、全局记忆），并且能改任何条目",
}


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    allow: Sequence[str]
    deny: Sequence[str]


def _of(name: str, row: RoleRow | None) -> Role:
    fixed = FIXED_RULES.get(name, Rules())
    if row is None:
        return Role(name, BUILTIN_DESCRIPTIONS[name], Rules(), fixed)
    return Role(name, row.description, Rules(tuple(row.allow), tuple(row.deny)), fixed)


async def roles_in(s: AsyncSession) -> dict[str, Role]:
    """全部角色：内置在前（按定义顺序），自建的按名称。"""
    rows = {r.name: r for r in await s.scalars(select(RoleRow).order_by(RoleRow.name))}
    builtin = {n: _of(n, rows.get(n)) for n in FIXED_RULES}
    return {**builtin, **{n: _of(n, r) for n, r in rows.items() if n not in FIXED_RULES}}


async def check_assignable_in(s: AsyncSession, names: Sequence[str]) -> None:
    """agent 可以选的角色：存在、不重复，不含 base（每个 agent 隐含）。"""
    if BASE in names:
        raise InvalidInput(f"role {BASE!r} is implicit for every agent; do not list it in roles")
    roles = await roles_in(s)
    unknown = [n for n in names if n not in roles]
    if unknown or len(set(names)) != len(names):
        assignable = [r for r in roles if r != BASE]
        raise InvalidInput(f"roles must be distinct names from {assignable}, got {list(names)}")


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


def _check(spec: RoleSpec) -> None:
    validate_slug("role", spec.name)
    if not spec.description.strip():
        raise InvalidInput("role description must not be empty")


class RoleService:
    def __init__(self, db: Database):
        self._db = db

    async def list(self) -> list[Role]:
        async with self._db.transaction() as s:
            return list((await roles_in(s)).values())

    async def get(self, name: str) -> Role:
        async with self._db.transaction() as s:
            role = (await roles_in(s)).get(name)
            if role is None:
                raise NotFound(f"role {name!r} not found")
            return role

    async def create(self, spec: RoleSpec) -> Role:
        _check(spec)
        async with self._db.transaction() as s:
            if spec.name in await roles_in(s):
                raise Conflict(f"role {spec.name!r} already exists")
            await check_rules_in(s, Rules(tuple(spec.allow), tuple(spec.deny)))
            row = RoleRow(
                name=spec.name, description=spec.description, allow=list(spec.allow), deny=list(spec.deny)
            )
            s.add(row)
            await s.flush()
            return _of(row.name, row)

    async def update(self, spec: RoleSpec) -> Role:
        """整体替换描述与可编辑的规则（名称不改：agent 按名称引用它）；内置角色也可以（固定的部分不变）。改动从带这个角色
        的 agent 的下一次运行起生效。"""
        async with self._db.transaction() as s:
            return await self._update_in(s, spec)

    @staticmethod
    async def _update_in(s: AsyncSession, spec: RoleSpec) -> Role:
        _check(spec)
        row = await s.get(RoleRow, spec.name)
        if row is None and spec.name not in FIXED_RULES:
            raise NotFound(f"role {spec.name!r} not found")
        before = Rules() if row is None else Rules(tuple(row.allow), tuple(row.deny))
        await check_rules_in(s, Rules(tuple(spec.allow), tuple(spec.deny)), before)
        if row is None:
            row = RoleRow(name=spec.name)
            s.add(row)
        row.description, row.allow, row.deny = spec.description, list(spec.allow), list(spec.deny)
        await s.flush()
        return _of(row.name, row)

    async def patch(
        self,
        name: str,
        *,
        description: str | None = None,
        allow: Sequence[str] | None = None,
        deny: Sequence[str] | None = None,
    ) -> Role:
        """只改给出的字段（None 为不改），读与写在同一个事务里：并发的局部修改互不覆盖。规则同 update。"""
        async with self._db.transaction() as s:
            role = (await roles_in(s)).get(name)
            if role is None:
                raise NotFound(f"role {name!r} not found")
            spec = RoleSpec(
                name,
                role.description if description is None else description,
                role.rules.allow if allow is None else allow,
                role.rules.deny if deny is None else deny,
            )
            return await self._update_in(s, spec)

    async def delete(self, name: str) -> None:
        async with self._db.transaction() as s:
            if name in FIXED_RULES:
                raise InvalidInput(f"role {name!r} is built in and cannot be deleted")
            row = await s.get(RoleRow, name)
            if row is None:
                raise NotFound(f"role {name!r} not found")
            # 包括正在删除的 agent：它的行还在，删除流程完成前不让角色先消失
            users = [a.name for a in await s.scalars(select(Agent)) if name in a.roles]
            if users:
                raise Conflict(f"role {name!r} is used by agents {users}")
            await s.delete(row)


async def rules_in(s: AsyncSession, roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]) -> Rules:
    """agent 的最终规则：base + 它的角色 + 它自己的 allow / deny。读到不存在的角色说明数据与代码不一致（写入时已校验、
    被引用的角色不能删；内置角色从代码里去掉时启动检查会报出）。"""
    known = await roles_in(s)
    missing = [r for r in roles if r not in known]
    if missing:
        raise RuntimeError(f"agent references unknown roles {missing}")
    return Rules.union([*(known[r].effective for r in (BASE, *roles)), Rules(tuple(allow), tuple(deny))])
