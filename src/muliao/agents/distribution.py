"""分发子域的门面（Distribution）：角色的增删改与查看，agent 的规则与它最终能用的（reach），配置的预览，启动时的规则检查。

子域的事务内部分各在一处：角色 agents.roles，agent 的规则 agents.agent_rules（建、改随目录子域的 agent 写入），能用 / 能改与
由此派生的 agents.access。每次修改在同一个写事务里记一版（revisions：角色的文档是描述与可编辑的规则，agent 的是它的
roles / allow / deny），作者是改的人。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.access import Reach, granted_by, grants_in, grants_of, reach_in
from muliao.agents.active import active_row_in, agent_names_in
from muliao.agents.agent_rules import (
    AgentRules,
    agent_rules_in,
    all_agent_rules_in,
    check_agent_rules_in,
    holders_in,
)
from muliao.agents.kinds import Subject, label
from muliao.agents.naming import validate_slug
from muliao.agents.permissions import FIXED_RULES, Role, Rules, check_patterns
from muliao.agents.revisions import record_in
from muliao.agents.roles import check_rules_in, document, role_of, roles_in
from muliao.agents.tables import Role as RoleRow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    allow: Sequence[str]
    deny: Sequence[str]


@dataclass(frozen=True)
class RoleView:
    """角色页的一项：角色、只按它的规则能用的、带它的 agent。"""

    role: Role
    reach: Reach
    # 带这个角色的 agent 的 id（agent_rules.holders_in：base 是全部，含归档的）
    holders: tuple[str, ...]


def _check(spec: RoleSpec) -> None:
    validate_slug(label(Subject.ROLE), spec.name)
    if not spec.description.strip():
        raise InvalidInput("role description must not be empty")


async def _role_in(s: AsyncSession, name: str) -> Role:
    role = (await roles_in(s)).get(name)
    if role is None:
        raise NotFound(f"role {name!r} not found")
    return role


class Distribution:
    def __init__(
        self,
        db: Database,
        # 当前配置下注册的工具（permissions.available_tools）：能用的工具只在它里面取
        tools: Sequence[str],
    ):
        self._db = db
        self.tools = tuple(tools)

    # ---- agent 的规则 ----

    async def agent_rules(self, agent_id: str) -> AgentRules:
        """agent 的规则（包括正在删除的）。"""
        async with self._db.transaction() as s:
            return await agent_rules_in(s, agent_id)

    async def all_agent_rules(self) -> dict[str, AgentRules]:
        """全部 agent 的规则（包括正在删除的），按 id。"""
        async with self._db.transaction() as s:
            return await all_agent_rules_in(s)

    async def rules(self, agent_id: str) -> Rules:
        """agent 的最终规则（base + 角色 + 自己的 allow / deny），按角色现在的定义算：运行开放哪些工具
        （agent_runtime.Toolbox.profile）。"""
        async with self._db.transaction() as s:
            return (await grants_in(s, agent_id)).rules

    async def reach(self, agent_id: str) -> Reach:
        """agent 最终能用的内置工具与 skill、MCP server、secret（agents.access 的规则 1），一个事务里按角色现在的定义算：
        get_agent 与界面同一套计算；运行时（agent_runtime.Toolbox）按实际注册的工具集算，两者一致由组合根启动时核对
        available_tools 与注册的工具保证（container._check_permissions）。"""
        async with self._db.transaction() as s:
            grants = await grants_in(s, agent_id)
            return await reach_in(s, self.tools, grants.rules, grants.uses)

    async def preview_agent(self, agent_id: str | None, rules: AgentRules) -> Reach:
        """agent（None 为还没建的）按还没保存的规则能用的内置工具与条目（界面预览），与写入时同样校验：已存的规则算作
        「之前的」，不要求仍匹配现有条目。"""
        async with self._db.transaction() as s:
            if agent_id is not None:
                await active_row_in(s, agent_id)
            before = AgentRules() if agent_id is None else await agent_rules_in(s, agent_id)
            await check_agent_rules_in(s, rules, before)
            known = await roles_in(s)
            # 还没建的 agent 没有自己的条目，只看规则；已有的按 agents.access 的规则 1
            if agent_id is None:
                effective = rules.effective(known)
                return await reach_in(s, self.tools, effective, granted_by(effective))
            grants = grants_of(agent_id, rules, known)
            return await reach_in(s, self.tools, grants.rules, grants.uses)

    async def verify_stored(self) -> None:
        """启动检查：角色与 agent 的规则形式合法且 tool 规则仍匹配登记的工具，agent 引用的角色都在（且不含隐含的 base），与
        写入时同一套规则（skill / mcp / secret 规则不查是否匹配现有条目：条目可以删了再建）。代码改动（工具改名、删内置
        角色）在启动时暴露，而不是散落到每次运行里失败。正在删除的 agent 也查（它的规则还会被 sharing_in 读到）。"""
        async with self._db.transaction() as s:
            roles = await roles_in(s)
            agents = await all_agent_rules_in(s)
            names = await agent_names_in(s)
        broken = []
        for owner, rules in [
            *((f"role {r.name}", r.rules) for r in roles.values()),
            *((f"agent {names[a]} ({a})", r.own) for a, r in agents.items()),
        ]:
            try:
                check_patterns(rules.allow)
                check_patterns(rules.deny)
            except InvalidInput as e:
                broken.append(f"{owner}: {e}")
        broken += [
            f"agent {names[a]} ({a}): unknown roles {unknown}"
            for a, r in agents.items()
            if (unknown := [n for n in r.roles if n not in roles or not roles[n].assignable])
        ]
        if broken:
            raise RuntimeError("stored tool permissions are invalid:\n" + "\n".join(broken))

    # ---- 角色 ----

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
            role = role_of(name, row)
            holders = await holders_in(s, role)
            if holders:
                names = await agent_names_in(s)
                raise Conflict(f"role {name!r} is used by agents {[names[a] for a in holders]}")
            record_in(s, Subject.ROLE, name, document(role), None, actor, f"delete role {name}")
            await s.delete(row)

    async def views(self) -> list[RoleView]:
        """全部角色的 RoleView（顺序同 list），一个事务里算完。"""
        async with self._db.transaction() as s:
            return [await self._view_in(s, r) for r in (await roles_in(s)).values()]

    async def view(self, name: str) -> RoleView:
        async with self._db.transaction() as s:
            return await self._view_in(s, await _role_in(s, name))

    async def _view_in(self, s: AsyncSession, role: Role) -> RoleView:
        """reach 只按角色自己的规则（固定的与可编辑的），不含任何 agent 自己的条目。"""
        return RoleView(
            role,
            await reach_in(s, self.tools, role.effective, granted_by(role.effective)),
            tuple(await holders_in(s, role)),
        )

    async def preview_role(self, name: str | None, allow: Sequence[str], deny: Sequence[str]) -> Reach:
        """角色（None 为还没建的）按还没保存的规则能用的（界面预览），与写入时同样校验；内置角色加上它固定的部分。"""
        async with self._db.transaction() as s:
            role = None if name is None else await _role_in(s, name)
            rules = Rules(tuple(allow), tuple(deny))
            await check_rules_in(s, rules, Rules() if role is None else role.rules)
            effective = rules if role is None else Rules.union([role.fixed, rules])
            return await reach_in(s, self.tools, effective, granted_by(effective))
