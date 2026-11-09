"""AI 资源池（ItemCatalog）：跨种类看资源——全部资源与收录它们的角色（界面的资源页）、转移 owner（把文件或目录挪到新 owner
的目录下）。各种类自己的内容与写入在各 store（skills、mcp、secrets、memory）；能用、能改的规则是分发子域的（agents.access）。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from muliao.agents import assets
from muliao.agents.access import changeable, sharing_of
from muliao.agents.kinds import Kind, StoredKind, as_kind, label
from muliao.agents.mcp import McpServer, server_of
from muliao.agents.permissions import ToolSpec, spec_of
from muliao.agents.source import AssetSource
from muliao.errors import InvalidInput, NotFound

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogEntry:
    """资源池里的一项（界面的资源页）。内置工具归平台、没有描述、修改时间与文件。"""

    kind: Kind
    name: str
    owner: str | None
    description: str
    updated_at: datetime | None
    # 收录它的角色（含内置的，permissions.Rules.allows）
    roles: tuple[str, ...]
    # 分发出去了（agents.access.Sharing.shared）：否则只有 owner 能用（归平台的就没有 agent 能用）
    shared: bool
    # 文件或目录的位置；内置工具为 None
    path: Path | None = None
    # 内置工具的读写与是否管理类；别的种类为 None
    tool: ToolSpec | None = None
    # MCP server 的配置与启用状态；别的种类为 None
    mcp: McpServer | None = None


class ItemCatalog:
    def __init__(
        self,
        source: AssetSource,
        # 当前配置下注册的工具（permissions.available_tools）
        tools: Sequence[str],
    ):
        self._source = source
        self._tools = tuple(tools)

    async def catalog(self) -> list[CatalogEntry]:
        """全部资源：内置工具（按登记顺序）在前，skill、MCP server、secret、记忆按种类、名称；每项带收录它的角色。"""
        snapshot = await self._source.snapshot()
        sharing = sharing_of(snapshot)
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
                as_kind(kind),
                a.name,
                a.owner,
                a.description,
                a.updated_at,
                sharing.including(as_kind(kind), a.name),
                sharing.shared(as_kind(kind), a.name, a.owner),
                path=a.path,
                mcp=server_of(a) if kind == StoredKind.MCP else None,
            )
            for kind in sorted(StoredKind, key=lambda k: k.value)
            for a in snapshot.of_kind(kind)
        ]

    async def transfer_item(self, kind: StoredKind, name: str, owner: str | None, actor: str) -> None:
        """把资源挪到 owner（agent 的 id；None 归平台）的目录下。actor 要能改它（Grants.can 的 write）。内容不变；谁能用随
        owner 变（agents.access 的 Grants.can）。agent 的私有记忆名字就是 agent 名，所以记忆只能在平台与同名 agent 之间转。"""
        async with self._source.writing() as snapshot:
            asset = changeable(snapshot, kind, name, actor)
            if asset.owner == owner:
                raise InvalidInput(f"{label(kind)} {name!r} already belongs to {snapshot.owner_label(owner)}")
            if owner is not None and owner not in snapshot.agents:
                raise NotFound(f"agent {owner!r} not found")
            if kind == StoredKind.MEMORY and owner is not None and snapshot.agents[owner].name != name:
                raise InvalidInput(
                    f"an agent's private memory is named after the agent; memory {name!r} cannot go to "
                    f"{snapshot.owner_label(owner)}"
                )
            await asyncio.to_thread(assets.move, asset.path, snapshot.path_of(kind, name, owner))
        log.info("item transferred", extra={"kind": kind, "item": name, "owner": owner, "actor": actor})
