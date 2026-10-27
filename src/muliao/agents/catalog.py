"""条目池（ItemCatalog，条目子域的门面）：跨种类看条目——全部条目与收录它们的角色（界面的条目页）、转移 owner、版本历史与恢复。
各种类自己的内容与写入在各 store（skills、mcp、secrets）；owner 的变更在 agents.ownership；能用、能改的规则与一个 agent 能用的
（reach）是分发子域的（agents.access、agents.distribution）。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from muliao.agents.access import changeable_in, sharing_in
from muliao.agents.active import active_row_in
from muliao.agents.items import check_visible_in, owner_label_in, visible_in
from muliao.agents.kinds import Kind, StoredKind, Subject, as_subject, label
from muliao.agents.mcp import McpRegistry, McpServer, server_of
from muliao.agents.ownership import transfer_in
from muliao.agents.permissions import ToolSpec, spec_of
from muliao.agents.revisions import RESTORABLE, RevisionInfo, history_in
from muliao.agents.skills import SkillStore
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
    # 分发出去了（agents.access.Sharing.shared）：否则只有 owner 能用（归平台的就没有 agent 能用）
    shared: bool
    # 内置工具的读写与是否管理类；别的种类为 None
    tool: ToolSpec | None = None
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
        if {as_subject(k) for k in self._restore} != RESTORABLE - {Subject.MEMORY}:
            raise RuntimeError(
                f"restorable item kinds {sorted(self._restore)} do not match revisions.RESTORABLE"
            )

    async def catalog(self) -> list[CatalogEntry]:
        """全部条目：内置工具（按登记顺序）在前，skill、MCP server、secret 按种类、名称；每项带收录它的角色。"""
        async with self._db.transaction() as s:
            sharing = await sharing_in(s)
            # 正在删除的 agent 的条目按不存在处理（agents.items）
            items = await visible_in(s)
            tools = [
                CatalogEntry(
                    Kind.TOOL,
                    n,
                    None,
                    "",
                    None,
                    sharing.including(Kind.TOOL, n),
                    sharing.shared(Kind.TOOL, n, None),
                    tool=spec_of(n),
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
                    sharing.including(Kind(i.kind), i.name),
                    sharing.shared(Kind(i.kind), i.name, i.owner),
                    mcp=server_of(i) if i.kind == StoredKind.MCP else None,
                )
                for i in items
            ]

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
            await transfer_in(s, row, owner, actor)

    async def history(self, kind: StoredKind, name: str, limit: int) -> list[RevisionInfo]:
        """新的在前，第一条即当前的一版（已删除时是那次删除）。"""
        async with self._db.transaction() as s:
            await check_visible_in(s, kind, name)
            return await history_in(s, as_subject(kind), name, limit)

    async def restore(self, kind: StoredKind, name: str, rev: int, actor: str) -> None:
        """把第 rev 版的内容作为新的一版写入（归属不变）。secret 的版本不含值，不能恢复。"""
        if as_subject(kind) not in RESTORABLE:
            raise InvalidInput(f"{label(kind)} revisions cannot be restored")
        await self._restore[kind](name, rev, actor)
