"""角色：内置的（agents.permissions.BUILTIN_ROLES）加用户自建的（roles 表）。agent 按名称引用角色。

引用的一致性在写事务里保证：agent 写入角色名时在同一事务里检查角色存在，删除角色时在同一事务里检查没有 agent 引用它
（事务串行，两者不会交错）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.naming import validate_slug
from muliao.agents.permissions import BUILTIN_ROLES, Role, Rules, check_patterns
from muliao.agents.tables import Agent
from muliao.agents.tables import Role as RoleRow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    allow: Sequence[str]
    deny: Sequence[str]


def _of(row: RoleRow) -> Role:
    return Role(row.name, row.description, Rules(tuple(row.allow), tuple(row.deny)), builtin=False)


async def roles_in(s: AsyncSession) -> dict[str, Role]:
    """全部角色：内置在前（按定义顺序），自建的按名称。"""
    custom = await s.scalars(select(RoleRow).order_by(RoleRow.name))
    return {**BUILTIN_ROLES, **{r.name: _of(r) for r in custom}}


async def check_assignable_in(s: AsyncSession, names: Sequence[str]) -> None:
    """agent 可以选的角色：存在、不重复。"""
    roles = await roles_in(s)
    unknown = [n for n in names if n not in roles]
    if unknown or len(set(names)) != len(names):
        raise InvalidInput(f"roles must be distinct names from {list(roles)}, got {list(names)}")


def _check(spec: RoleSpec) -> None:
    validate_slug("role", spec.name)
    if not spec.description.strip():
        raise InvalidInput("role description must not be empty")
    check_patterns(spec.allow)
    check_patterns(spec.deny)


class RoleService:
    def __init__(self, db: Database):
        self._db = db

    async def list(self) -> list[Role]:
        async with self._db.transaction() as s:
            return list((await roles_in(s)).values())

    async def create(self, spec: RoleSpec) -> Role:
        _check(spec)
        async with self._db.transaction() as s:
            if spec.name in await roles_in(s):
                raise Conflict(f"role {spec.name!r} already exists")
            row = RoleRow(
                name=spec.name, description=spec.description, allow=list(spec.allow), deny=list(spec.deny)
            )
            s.add(row)
            await s.flush()
            return _of(row)

    async def update(self, spec: RoleSpec) -> Role:
        """整体替换描述与规则（名称不改：agent 按名称引用它）。改动从引用它的 agent 的下一次运行起生效。"""
        _check(spec)
        async with self._db.transaction() as s:
            row = await self._custom_in(s, spec.name)
            row.description, row.allow, row.deny = spec.description, list(spec.allow), list(spec.deny)
            await s.flush()
            return _of(row)

    async def delete(self, name: str) -> None:
        async with self._db.transaction() as s:
            row = await self._custom_in(s, name)
            # 包括正在删除的 agent：它的行还在，删除流程完成前不让角色先消失
            users = [a.name for a in await s.scalars(select(Agent)) if name in a.roles]
            if users:
                raise Conflict(f"role {name!r} is used by agents {users}")
            await s.delete(row)

    @staticmethod
    async def _custom_in(s: AsyncSession, name: str) -> RoleRow:
        if name in BUILTIN_ROLES:
            raise InvalidInput(f"role {name!r} is built in and cannot be changed")
        row = await s.get(RoleRow, name)
        if row is None:
            raise NotFound(f"role {name!r} not found")
        return row


async def rules_in(s: AsyncSession, roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]) -> Rules:
    """agent 的最终规则：它的角色 + 它自己的 allow / deny。读到不存在的角色说明数据与代码不一致（写入时已校验、
    被引用的角色不能删；内置角色从代码里去掉时启动检查会报出）。"""
    known = await roles_in(s)
    missing = [r for r in roles if r not in known]
    if missing:
        raise RuntimeError(f"agent references unknown roles {missing}")
    return Rules.union([*(known[r].rules for r in roles), Rules(tuple(allow), tuple(deny))])
