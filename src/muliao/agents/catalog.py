"""条目池（ItemCatalog）：跨种类看条目——全部条目与收录它们的角色（界面的条目页）、一个 agent 能用的（reach）、agent 配置的预览、
转移 owner、版本历史与恢复。各种类自己的内容与写入在各 store（skills、mcp、secrets）；能用、能改的规则在 agents.access。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from muliao.agents.access import Grants, granted_by, grants_in, owner_label_in, shared_in
from muliao.agents.active import active_row_in
from muliao.agents.documents import document_in
from muliao.agents.items import Reach, changeable_in, check_visible_in, reach_in
from muliao.agents.kinds import Kind, StoredKind, as_subject, label
from muliao.agents.mcp import McpRegistry, McpServer, server_of
from muliao.agents.permissions import Rules
from muliao.agents.revisions import RESTORABLE, RevisionInfo, history_in, record_in
from muliao.agents.roles import check_assignable_in, check_rules_in, roles_in, rules_in
from muliao.agents.skills import SkillStore
from muliao.agents.tables import Agent, Item
from muliao.errors import InvalidInput
from muliao.platform.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogEntry:
    """条目池里的一项（界面的条目页）。内置工具归平台、没有描述与修改时间。"""

    kind: Kind
    name: str
    owner: str | None
    description: str
    updated_at: datetime | None
    # 收录它的角色（含内置的，permissions.Rules.allows）
    roles: tuple[str, ...]
    # 分发出去了（agents.access.shared_in）：否则只有 owner 能用（归平台的就没有 agent 能用）
    shared: bool
    # MCP server 的配置与启用状态；别的种类为 None
    mcp: McpServer | None = None


class ItemCatalog:
    def __init__(
        self,
        db: Database,
        # 当前配置下注册的工具（permissions.available_tools）
        tools: Sequence[str],
        skills: SkillStore,
        mcp: McpRegistry,
    ):
        self._db = db
        self._tools = tuple(tools)
        # 能恢复旧版的种类（revisions.RESTORABLE）各自的恢复
        self._restore: dict[StoredKind, Callable[[str, int, str], Awaitable[object]]] = {
            StoredKind.SKILL: skills.restore,
            StoredKind.MCP: mcp.restore,
        }

    async def catalog(self) -> list[CatalogEntry]:
        """全部条目：内置工具（按登记顺序）在前，skill、MCP server、secret 按种类、名称；每项带收录它的角色。"""
        async with self._db.transaction() as s:
            roles = list((await roles_in(s)).values())
            shared = await shared_in(s)
            # 正在删除的 agent 的条目按不存在处理（agents.items）
            deleting = select(Agent.id).where(Agent.deleting_by.is_not(None))
            items = await s.scalars(
                select(Item)
                .where(Item.owner.is_(None) | Item.owner.not_in(deleting))
                .order_by(Item.kind, Item.name)
            )

            def including(kind: Kind, name: str) -> tuple[str, ...]:
                return tuple(r.name for r in roles if r.effective.allows(kind, name))

            tools = [
                CatalogEntry(
                    Kind.TOOL, n, None, "", None, including(Kind.TOOL, n), shared(Kind.TOOL, n, None)
                )
                for n in self._tools
            ]
            return tools + [
                CatalogEntry(
                    Kind(i.kind),
                    i.name,
                    i.owner,
                    i.description,
                    i.updated_at,
                    including(Kind(i.kind), i.name),
                    shared(Kind(i.kind), i.name, i.owner),
                    server_of(i) if i.kind == StoredKind.MCP else None,
                )
                for i in items
            ]

    async def reach(self, agent_id: str) -> Reach:
        """agent 最终能用的内置工具与 skill、MCP server、secret（agents.access 的规则 1），一个事务里按角色现在的定义算：
        get_agent 与界面同一套计算；运行时（agent_runtime.Toolbox）按实际注册的工具集算，两者一致由组合根启动时核对
        available_tools 与注册的工具保证（container._check_permissions）。"""
        async with self._db.transaction() as s:
            grants = await grants_in(s, agent_id)
            return await reach_in(s, self._tools, grants.rules, grants.uses)

    async def preview(
        self, agent_id: str | None, roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]
    ) -> Reach:
        """agent（None 为还没建的）按还没保存的配置能用的内置工具与条目（界面预览），与写入时同样校验：已存的规则算作
        「之前的」，不要求仍匹配现有条目。"""
        async with self._db.transaction() as s:
            row = None if agent_id is None else await active_row_in(s, agent_id)
            before = Rules() if row is None else Rules(tuple(row.allow), tuple(row.deny))
            await check_rules_in(s, Rules(tuple(allow), tuple(deny)), before)
            await check_assignable_in(s, roles)
            rules = await rules_in(s, roles, allow, deny)
            # 还没建的 agent 没有自己的条目，只看规则；已有的按 agents.access 的规则 1
            keep = granted_by(rules) if agent_id is None else Grants.of(agent_id, roles, rules).uses
            return await reach_in(s, self._tools, rules, keep)

    async def transfer_item(self, kind: StoredKind, name: str, owner: str | None, actor: str) -> None:
        """把条目转给 owner（agent 的 id；None 归平台），记一版。actor 要能改它（规则 2），新 owner 要还可用（归档的也行：
        条目照常可改）。内容不变；谁能用随 owner 变（agents.access 的规则 1）。"""
        async with self._db.transaction() as s:
            row = await changeable_in(s, kind, name, actor)
            if row.owner == owner:
                raise InvalidInput(
                    f"{label(kind)} {name!r} already belongs to {await owner_label_in(s, owner)}"
                )
            if owner is not None:
                await active_row_in(s, owner)
            message = (
                f"transfer {label(kind)} {name} from {await owner_label_in(s, row.owner)} "
                f"to {await owner_label_in(s, owner)}"
            )
            doc = await document_in(s, row)
            # 文档不含 owner：前后相同，照样记一版
            record_in(s, as_subject(kind), name, doc, doc, actor, message, force=True)
            row.owner = owner
        log.info("item transferred", extra={"kind": kind.value, "item": name, "owner": owner, "actor": actor})

    async def history(self, kind: StoredKind, name: str, limit: int) -> list[RevisionInfo]:
        """新的在前，第一条即当前的一版（已删除时是那次删除）。"""
        async with self._db.transaction() as s:
            await check_visible_in(s, kind, name)
            return await history_in(s, as_subject(kind), name, limit)

    async def restore(self, kind: StoredKind, name: str, rev: int, author: str) -> None:
        """把第 rev 版的内容作为新的一版写入（归属不变）。secret 的版本不含值，不能恢复。"""
        if as_subject(kind) not in RESTORABLE:
            raise InvalidInput(f"{label(kind)} revisions cannot be restored")
        await self._restore[kind](name, rev, author)
