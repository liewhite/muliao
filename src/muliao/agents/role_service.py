"""角色的增删改与查看（agents.roles 是事务内的查询与校验）：每次修改在同一个写事务里记一版（revisions，subject role，
文档是描述与可编辑的规则），作者是改的人；预览与覆盖范围按 agents.items.reach_in 算（只看规则，不含任何 agent 自己的条目）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.access import granted_by
from muliao.agents.items import Reach, reach_in
from muliao.agents.kinds import Subject
from muliao.agents.naming import validate_slug
from muliao.agents.permissions import FIXED_RULES, Role, Rules
from muliao.agents.revisions import record_in
from muliao.agents.roles import check_rules_in, document, role_of, roles_in
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


def _check(spec: RoleSpec) -> None:
    validate_slug("role", spec.name)
    if not spec.description.strip():
        raise InvalidInput("role description must not be empty")


async def _role_in(s: AsyncSession, name: str) -> Role:
    role = (await roles_in(s)).get(name)
    if role is None:
        raise NotFound(f"role {name!r} not found")
    return role


class RoleService:
    def __init__(
        self,
        db: Database,
        # 当前配置下注册的工具（permissions.available_tools）：覆盖范围只在它里面取
        tools: Sequence[str],
    ):
        self._db = db
        self._tools = tuple(tools)

    async def list(self) -> list[Role]:
        async with self._db.transaction() as s:
            return list((await roles_in(s)).values())

    async def get(self, name: str) -> Role:
        async with self._db.transaction() as s:
            return await _role_in(s, name)

    async def create(self, spec: RoleSpec, *, actor: str) -> Role:
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
            role = role_of(row.name, row)
            record_in(s, Subject.ROLE, spec.name, None, document(role), actor, f"create role {spec.name}")
            return role

    async def update(self, spec: RoleSpec, *, actor: str) -> Role:
        """整体替换描述与可编辑的规则（名称不改：agent 按名称引用它）；内置角色也可以（固定的部分不变）。改动从带这个角色
        的 agent 的下一次运行起生效。"""
        async with self._db.transaction() as s:
            return await self._update_in(s, spec, actor)

    @staticmethod
    async def _update_in(s: AsyncSession, spec: RoleSpec, actor: str) -> Role:
        _check(spec)
        row = await s.get(RoleRow, spec.name)
        if row is None and spec.name not in FIXED_RULES:
            raise NotFound(f"role {spec.name!r} not found")
        before = role_of(spec.name, row)
        await check_rules_in(s, Rules(tuple(spec.allow), tuple(spec.deny)), before.rules)
        if row is None:
            row = RoleRow(name=spec.name)
            s.add(row)
        row.description, row.allow, row.deny = spec.description, list(spec.allow), list(spec.deny)
        await s.flush()
        role = role_of(row.name, row)
        record_in(
            s, Subject.ROLE, spec.name, document(before), document(role), actor, f"update role {spec.name}"
        )
        return role

    async def patch(
        self,
        name: str,
        *,
        actor: str,
        description: str | None = None,
        allow: Sequence[str] | None = None,
        deny: Sequence[str] | None = None,
    ) -> Role:
        """只改给出的字段（None 为不改），读与写在同一个事务里：并发的局部修改互不覆盖。规则同 update。"""
        async with self._db.transaction() as s:
            role = await _role_in(s, name)
            spec = RoleSpec(
                name,
                role.description if description is None else description,
                role.rules.allow if allow is None else allow,
                role.rules.deny if deny is None else deny,
            )
            return await self._update_in(s, spec, actor)

    async def delete(self, name: str, *, actor: str) -> None:
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
            record_in(s, Subject.ROLE, name, document(role_of(name, row)), None, actor, f"delete role {name}")
            await s.delete(row)

    async def reach(self, role: Role) -> Reach:
        """只按这个角色的规则（固定的与可编辑的）能用的内置工具与条目（不含任何 agent 自己的条目）。"""
        async with self._db.transaction() as s:
            return await reach_in(s, self._tools, role.effective, granted_by(role.effective))

    async def preview(self, name: str | None, allow: Sequence[str], deny: Sequence[str]) -> Reach:
        """角色（None 为还没建的）按还没保存的规则能用的（界面预览），与写入时同样校验；内置角色加上它固定的部分。"""
        async with self._db.transaction() as s:
            role = None if name is None else await _role_in(s, name)
            rules = Rules(tuple(allow), tuple(deny))
            await check_rules_in(s, rules, Rules() if role is None else role.rules)
            effective = rules if role is None else Rules.union([role.fixed, rules])
            return await reach_in(s, self._tools, effective, granted_by(effective))
