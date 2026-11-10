"""记忆：AI 资源的一种，一块有长度上限的 markdown 文件。agent 能用（use）的每一块，每次运行全文放进 instructions；能改
（write）的才能改。谁能用、能改是分发子域的规则（agents.access）。

三类记忆，规则相同，只是放的位置与分发不同（agents.assets）：
- 全局记忆（GLOBAL_MEMORY = global，`platform/memory/global.md`）：归平台，base 固定收录（use）；admin 靠 `write:*` 能改。
  不能删。
- 共享记忆（如 `platform/memory/trading.md`）：归平台，收录它的角色给 `*:memory:<名>`，成员都能用也都能改。
- 私有记忆（`<agent 名>/memory.md`）：归这个 agent，记忆名就是 agent 名（改名跟着变）；建 agent 时建一份空的。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from muliao.agents import assets, memory_file
from muliao.agents.access import grants_for, usable_by
from muliao.agents.kinds import Kind, StoredKind, label
from muliao.agents.permissions import GLOBAL_MEMORY
from muliao.agents.source import AssetSource
from muliao.errors import InvalidInput

_KIND = StoredKind.MEMORY
_LABEL = label(_KIND)


@dataclass(frozen=True)
class MemoryBlock:
    """一次运行放进 instructions 的一块记忆。"""

    name: str
    content: str
    # 这个 agent 能改它（Grants.can 的 write）
    editable: bool
    # 归这个 agent（它的私有记忆）
    yours: bool
    # 文件位置
    path: Path


@dataclass(frozen=True)
class MemoryInfo:
    name: str
    owner: str | None
    content: str
    updated_at: datetime
    path: Path
    # content 的版本（assets.version_of）：整体改写时交回
    version: str


class MemoryStore:
    def __init__(self, source: AssetSource):
        self._source = source

    @property
    def max_chars(self) -> int:
        """每一块各自的上限。"""
        return self._source.memory_max_chars

    async def read(self, name: str) -> MemoryInfo:
        a = (await self._source.snapshot()).asset(_KIND, name)
        return MemoryInfo(a.name, a.owner, a.text, a.updated_at, a.path, assets.version_of(a.text))

    async def for_run(self, agent_id: str) -> list[MemoryBlock]:
        """组装一次运行的指令时读：这个 agent 能用的每一块（自己的与规则分来的），按名字。"""
        snapshot = await self._source.snapshot()
        grants = grants_for(snapshot, agent_id)
        return [
            MemoryBlock(
                a.name, a.text, grants.writes(Kind.MEMORY, a.name, a.owner), a.owner == agent_id, a.path
            )
            for a in usable_by(snapshot, _KIND, grants)
        ]

    async def put(
        self, name: str, content: str, *, version: str | None, owner: str | None = None
    ) -> MemoryInfo:
        """新建或整体改写（用户在界面上编辑）：已有的 owner 不变；新建的归 owner。
        version 是打开编辑时的版本（新建为 None），期间被别处改过就 Conflict（assets.check_version）。"""
        snapshot = await self._source.snapshot()
        existing = snapshot.existing(_KIND, name)
        path = existing.path if existing is not None else self._new_path(snapshot, name, owner)

        def unchanged(path: Path) -> None:
            # 此刻文件里的（写入口在锁里、紧挨着写之前调用）：期间被删、被抢先建、被改过都对不上
            current = memory_file.text_of(path) if path.is_file() else None
            assets.check_version(current, version, f"memory {name!r}")

        await self._source.write({path: content}, unchanged)
        return await self.read(name)

    def _new_path(self, snapshot: assets.Snapshot, name: str, owner: str | None) -> Path:
        """新建的位置；名字的规则（新建的平台记忆是 slug、不与别的重名）由写入口校验（asset_files.vet）。"""
        if owner is not None:
            agent = snapshot.agents.get(owner)
            if agent is None or agent.name != name:
                raise InvalidInput(
                    f"an agent's private memory is named after the agent ({agent.name if agent else owner!r}); "
                    "other memories belong to the platform"
                )
        return snapshot.path_of(_KIND, name, owner)

    async def delete(self, name: str) -> None:
        """全局记忆不能删（base 固定收录它）。"""
        if name == GLOBAL_MEMORY:
            raise InvalidInput(f"the global memory {GLOBAL_MEMORY!r} cannot be deleted")
        async with self._source.writing() as snapshot:
            asset = snapshot.asset(_KIND, name)
            await asyncio.to_thread(assets.remove, asset.path)
