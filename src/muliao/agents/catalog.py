"""AI 资源池（AssetCatalog）：跨种类看资源——全部资源与收录它们的角色（界面的资源页）——与各种类通用的写：删除、转移
owner（把文件或目录挪到新 owner 的目录下）。各种类自己的内容与写入在各 store（skills、mcp、secrets、memory）；能用、能改的
规则在分发（agents.access）。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from muliao.agents import assets
from muliao.agents.access import sharing_of
from muliao.agents.assets import McpFile, MemoryText, SkillDir
from muliao.agents.kinds import Kind, StoredKind, as_kind, label
from muliao.agents.permissions import GLOBAL_MEMORY, ToolSpec
from muliao.agents.secrets_format import SecretFile
from muliao.agents.source import AssetSource
from muliao.errors import Conflict, InvalidInput, NotFound

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretDetail:
    """资源池里 secret 的详情：只有 allow_shell（agents.secrets），密文不出 agent 领域。"""

    allow_shell: bool


# 种类专有的详情：内置工具的读写与是否管理类；存成文件的是它的内容（agents.assets.Content），secret 换成 SecretDetail
Detail = ToolSpec | MemoryText | SkillDir | McpFile | SecretDetail


@dataclass(frozen=True)
class CatalogAsset:
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
    path: Path | None
    detail: Detail


class AssetCatalog:
    def __init__(self, source: AssetSource):
        self._source = source

    async def catalog(self) -> list[CatalogAsset]:
        """全部资源：内置工具（当前配置下注册的，按注册顺序）在前，skill、MCP server、secret、记忆按种类、名称；每项带收录它的角色。"""
        snapshot = await self._source.snapshot()
        sharing = sharing_of(snapshot)
        tools = [
            CatalogAsset(
                Kind.TOOL,
                t.name,
                None,
                "",
                None,
                sharing.including(Kind.TOOL, t.name),
                sharing.shared(Kind.TOOL, t.name, None),
                None,
                t,
            )
            for t in snapshot.tools.specs
        ]
        return tools + [
            CatalogAsset(
                as_kind(kind),
                a.name,
                a.owner,
                a.description,
                a.updated_at,
                sharing.including(as_kind(kind), a.rule_name),
                sharing.shared(as_kind(kind), a.rule_name, a.owner),
                a.path,
                SecretDetail(a.content.allow_shell) if isinstance(a.content, SecretFile) else a.content,
            )
            for kind in sorted(StoredKind, key=lambda k: k.value)
            for a in snapshot.of_kind(kind)
        ]

    async def delete(self, kind: StoredKind, name: str) -> None:
        """删掉这项按名字认的资源的文件或目录（用户在界面上删；记忆只有平台的，私有记忆随 agent）。全局记忆不能删（base
        固定收录它）。收录它的规则留着：同名的再建时分发不丢（access_file.vet 只查新加的规则）。"""
        if kind == StoredKind.MEMORY and name == GLOBAL_MEMORY:
            raise InvalidInput(f"the global memory {GLOBAL_MEMORY!r} cannot be deleted")
        async with self._source.writing() as snapshot:
            asset = snapshot.asset(kind, name)
            await asyncio.to_thread(assets.remove, asset.path)
        log.info("asset deleted", extra={"kind": kind, "asset": name})

    async def transfer(self, kind: StoredKind, name: str, owner: str | None) -> None:
        """把资源挪到 owner（agent 的 id；None 归平台）的目录下（用户在界面上转）。内容不变；谁能用随
        owner 变（agents.access 的 Grants.can）。记忆不转移：平台的记忆归了 agent 就是第二块私有记忆（每个 agent 只有一块），私有记忆
        按所属 agent 认、在平台没有名字；要共享就把内容放进一块平台记忆。"""
        if kind == StoredKind.MEMORY:
            raise InvalidInput(
                "memories are not transferred: each agent has exactly one private memory, which belongs to it; to "
                "share content, put it into a platform memory"
            )
        async with self._source.writing() as snapshot:
            asset = snapshot.asset(kind, name)
            if asset.owner == owner:
                raise InvalidInput(f"{label(kind)} {name!r} already belongs to {snapshot.owner_label(owner)}")
            if owner is not None and owner not in snapshot.agents:
                raise NotFound(f"agent {owner!r} not found")
            copies = snapshot.shadowed[kind].get(name, ())
            if owner is not None and copies:
                # 平台那份遮住着 agent 目录里的同名副本（#502）：转给 agent 后就是 agent 之间同名，两边都不加载
                raise Conflict(
                    f"{label(kind)} {name!r} has copies in agent directories that the platform's one hides: "
                    f"{[str(p) for p in copies]}; once it belongs to an agent, the copies would clash with it and "
                    "none would load. Rename or remove the copies first"
                )
            await asyncio.to_thread(assets.move, asset.path, snapshot.path_of(kind, name, owner))
        log.info("asset transferred", extra={"kind": kind, "asset": name, "owner": owner})
