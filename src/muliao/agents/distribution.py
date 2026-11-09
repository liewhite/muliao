"""分发子域的门面（Distribution）：角色的增删改与查看，agent 的规则与它最终能用的（reach），配置的预览。

角色定义与各 agent 的角色、allow / deny 都在 access.toml（agents.assets 读，agents.access_file 写）；能用 / 能改与由此派生的
在 agents.access。读到的坏规则读树时就跳过并报出，这里不再有启动检查。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from muliao.agents import access_file
from muliao.agents.access import (
    Provenance,
    Reach,
    Source,
    WriteScope,
    granted_by,
    grants_for,
    grants_of,
    reach_of,
    usable_by,
    write_scope,
)
from muliao.agents.agent_rules import AgentRules
from muliao.agents.assets import Snapshot
from muliao.agents.kinds import StoredKind, as_kind
from muliao.agents.mcp import McpServer, server_of
from muliao.agents.naming import validate_slug
from muliao.agents.permissions import FIXED_RULES, Role, Rules
from muliao.agents.roles import check_assignable, check_rules, item_patterns
from muliao.agents.source import AssetSource
from muliao.errors import Conflict, InvalidInput, NotFound


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
    # 带这个角色的 agent 的 id（base 是全部）
    holders: tuple[str, ...]


@dataclass(frozen=True)
class UsedAsset:
    """agent 能用的一项资源（Distribution.assets）。"""

    kind: StoredKind
    name: str
    owner: str | None
    description: str
    # 为什么能用：它名下的 / 哪些角色 / 它自己的 allow
    source: Source
    # 能改（Grants.can 的 write）
    editable: bool
    # MCP server 的配置与启用状态；别的种类为 None
    mcp: McpServer | None


def _check(spec: RoleSpec) -> None:
    validate_slug("role", spec.name)
    if not spec.description.strip():
        raise InvalidInput("role description must not be empty")


def _role(snapshot: Snapshot, name: str) -> Role:
    role = snapshot.roles.get(name)
    if role is None:
        raise NotFound(f"role {name!r} not found")
    return role


def holders_of(snapshot: Snapshot, role: Role) -> list[str]:
    """带这个角色的 agent 的 id（base 每个 agent 都有）。"""
    return [a for a in snapshot.agents if role.held_by(snapshot.rules_of(a).roles)]


class Distribution:
    def __init__(
        self,
        source: AssetSource,
        # 当前配置下注册的工具（permissions.available_tools）：能用的工具只在它里面取
        tools: Sequence[str],
        # agent 还可用（正在删除的按不存在处理），否则 NotFound（agents.service.AgentService.get_active）
        check_agent: Callable[[str], Awaitable[object]],
    ):
        self._source = source
        self.tools = tuple(tools)
        self._check_agent = check_agent

    # ---- agent 的规则 ----

    async def rules(self, agent_id: str) -> Rules:
        """agent 的最终规则（base + 角色 + 自己的 allow / deny），按角色现在的定义算：运行开放哪些工具
        （agent_runtime.Toolbox.profile）。"""
        return grants_for(await self._source.snapshot(), agent_id).rules

    async def write_scope(self, agent_id: str) -> WriteScope:
        """agent 现在能写哪里（写沙箱 workspace.writable 用）：资源树按归属，加 `write:path` 授权。"""
        return write_scope(await self._source.snapshot(), agent_id)

    async def agent_rules(self, agent_id: str) -> AgentRules:
        """agent 自己的规则（角色与 allow / deny）。"""
        return (await self._source.snapshot()).rules_of(agent_id)

    async def reach(self, agent_id: str) -> Reach:
        """agent 最终能用的内置工具与 skill、MCP server、secret、记忆（agents.access 的 Grants.can）：get_agent 与界面同一套计算；
        运行时（agent_runtime.Toolbox）按实际注册的工具集算，两者一致由组合根启动时核对 available_tools 与注册的工具保证。"""
        snapshot = await self._source.snapshot()
        grants = grants_for(snapshot, agent_id)
        return reach_of(snapshot, self.tools, grants.rules, grants.uses)

    async def assets(self, agent_id: str) -> list[UsedAsset]:
        """agent 能用的 skill、MCP server、secret、记忆（按种类、名称），每项带来源与能不能改：界面的 agent 详情。与运行时同一
        套判断，来源由 access.Provenance 解释，两者对不上即报错。"""
        await self._check_agent(agent_id)
        snapshot = await self._source.snapshot()
        rules, known = snapshot.rules_of(agent_id), snapshot.roles
        grants = grants_of(agent_id, rules, known)
        provenance = Provenance.of(agent_id, rules, known)
        used = []
        for kind in StoredKind:
            for a in usable_by(snapshot, kind, grants):
                k = as_kind(kind)
                source = provenance.explain(k, a.name, a.owner)
                if not (source.own or source.roles or source.allow):
                    raise RuntimeError(f"no source explains why {agent_id} can use {kind} {a.name!r}")
                used.append(
                    UsedAsset(
                        kind,
                        a.name,
                        a.owner,
                        a.description,
                        source,
                        grants.writes(k, a.name, a.owner),
                        server_of(a) if kind == StoredKind.MCP else None,
                    )
                )
        return used

    async def preview_agent(self, agent_id: str | None, rules: AgentRules) -> Reach:
        """agent（None 为还没建的）按还没保存的规则能用的内置工具与资源（界面预览），与写入时同样校验：已存的规则算作
        「之前的」，不要求仍匹配现有资源。"""
        if agent_id is not None:
            await self._check_agent(agent_id)
        snapshot = await self._source.snapshot()
        before = AgentRules() if agent_id is None else snapshot.rules_of(agent_id)
        check_agent_rules(snapshot, rules, before)
        known = snapshot.roles
        # 还没建的 agent 没有自己的资源，只看规则；已有的按 agents.access 的 Grants.can
        if agent_id is None:
            effective = rules.effective(known)
            return reach_of(snapshot, self.tools, effective, granted_by(effective))
        grants = grants_of(agent_id, rules, known)
        return reach_of(snapshot, self.tools, grants.rules, grants.uses)

    # ---- 角色 ----

    async def list(self) -> list[Role]:
        return list((await self._source.snapshot()).roles.values())

    async def get(self, name: str) -> Role:
        return _role(await self._source.snapshot(), name)

    async def create(self, spec: RoleSpec, *, actor: str) -> Role:
        _check(spec)
        async with self._source.writing() as snapshot:
            if spec.name in snapshot.roles:
                raise Conflict(f"role {spec.name!r} already exists")
            await self._write_role(snapshot, spec, Rules())
        return await self.get(spec.name)

    async def update(self, spec: RoleSpec, *, actor: str) -> Role:
        """整体替换描述与可编辑的规则（名称不改：agent 按名称引用它）；内置角色也可以（固定的部分不变）。改动从带这个角色
        的 agent 的下一次运行起生效。"""
        _check(spec)
        async with self._source.writing() as snapshot:
            await self._write_role(snapshot, spec, _role(snapshot, spec.name).rules)
        return await self.get(spec.name)

    async def patch(
        self,
        name: str,
        *,
        actor: str,
        description: str | None = None,
        allow: Sequence[str] | None = None,
        deny: Sequence[str] | None = None,
    ) -> Role:
        """只改给出的字段（None 为不改），读与写串行：并发的局部修改互不覆盖。规则同 update。"""
        async with self._source.writing() as snapshot:
            role = _role(snapshot, name)
            spec = RoleSpec(
                name,
                role.description if description is None else description,
                role.rules.allow if allow is None else allow,
                role.rules.deny if deny is None else deny,
            )
            _check(spec)
            await self._write_role(snapshot, spec, role.rules)
        return await self.get(name)

    async def _write_role(self, snapshot: Snapshot, spec: RoleSpec, before: Rules) -> None:
        check_rules(item_patterns(snapshot.assets), Rules(tuple(spec.allow), tuple(spec.deny)), before)
        await asyncio.to_thread(
            access_file.edit,
            snapshot.access_file,
            lambda doc: access_file.set_role(
                doc, spec.name, spec.description, list(spec.allow), list(spec.deny)
            ),
        )

    async def delete(self, name: str, *, actor: str) -> None:
        if name in FIXED_RULES:
            raise InvalidInput(f"role {name!r} is built in and cannot be deleted")
        async with self._source.writing() as snapshot:
            role = _role(snapshot, name)
            holders = holders_of(snapshot, role)
            if holders:
                raise Conflict(
                    f"role {name!r} is used by agents {[snapshot.agents[a].name for a in holders]}"
                )
            await asyncio.to_thread(
                access_file.edit, snapshot.access_file, lambda doc: access_file.delete_role(doc, name)
            )

    async def views(self) -> list[RoleView]:
        """全部角色的 RoleView（顺序同 list）。"""
        snapshot = await self._source.snapshot()
        return [self._view(snapshot, r) for r in snapshot.roles.values()]

    async def view(self, name: str) -> RoleView:
        snapshot = await self._source.snapshot()
        return self._view(snapshot, _role(snapshot, name))

    def _view(self, snapshot: Snapshot, role: Role) -> RoleView:
        """reach 只按角色自己的规则（固定的与可编辑的），不含任何 agent 自己的资源。"""
        return RoleView(
            role,
            reach_of(snapshot, self.tools, role.effective, granted_by(role.effective)),
            tuple(holders_of(snapshot, role)),
        )

    async def preview_role(self, name: str | None, allow: Sequence[str], deny: Sequence[str]) -> Reach:
        """角色（None 为还没建的）按还没保存的规则能用的（界面预览），与写入时同样校验；内置角色加上它固定的部分。"""
        snapshot = await self._source.snapshot()
        role = None if name is None else _role(snapshot, name)
        rules = Rules(tuple(allow), tuple(deny))
        check_rules(item_patterns(snapshot.assets), rules, Rules() if role is None else role.rules)
        effective = rules if role is None else Rules.union([role.fixed, rules])
        return reach_of(snapshot, self.tools, effective, granted_by(effective))


def check_agent_rules(snapshot: Snapshot, rules: AgentRules, before: AgentRules = AgentRules()) -> None:
    """写入 agent 的规则之前：角色可选、规则合法且新加的匹配现有资源。"""
    check_assignable(snapshot.roles, rules.roles)
    check_rules(item_patterns(snapshot.assets), rules.own, before.own)
