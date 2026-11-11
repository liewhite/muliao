"""记忆：AI 资源的一种，一块有长度上限的 markdown 文件。agent 能用（use）的每一块，每次运行全文放进 instructions；能改
（write）的才能改。谁能用、能改是分发子域的规则（agents.access）。

三类记忆，只是放的位置与分发不同（agents.assets）：
- 全局记忆（GLOBAL_MEMORY = global，`platform/memory/global.md`）：归平台，base 固定收录（use）；admin 靠 `write:*` 能改。
  不能删。
- 共享记忆（如 `platform/memory/trading.md`）：归平台，收录它的角色给 `*:memory:<名>`，成员都能用也都能改。
- 私有记忆（`<agent 名>/memory.md`）：按所属的 agent 认（不占记忆的名字，显示的名字是 agent 名），只有它自己用、自己改
  （以及 admin）；规则不按名字指它。建 agent 时建一份空的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from muliao.agents import assets, memory_file
from muliao.agents.access import grants_for, usable_by
from muliao.agents.kinds import StoredKind
from muliao.agents.source import AssetSource
from muliao.errors import NotFound

_KIND = StoredKind.MEMORY


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


def _text(a: assets.Asset) -> str:
    return assets.content_of(a, assets.MemoryText).text


def _info(a: assets.Asset) -> MemoryInfo:
    text = _text(a)
    return MemoryInfo(a.name, a.owner, text, a.updated_at, a.path, assets.version_of(text))


class MemoryStore:
    def __init__(self, source: AssetSource):
        self._source = source

    @property
    def max_chars(self) -> int:
        """每一块各自的上限。"""
        return self._source.memory_max_chars

    async def read(self, name: str) -> MemoryInfo:
        """平台的一块记忆（全局或共享）。"""
        return _info((await self._source.snapshot()).asset(_KIND, name))

    async def read_private(self, agent_id: str) -> MemoryInfo:
        """agent 的私有记忆。"""
        snapshot = await self._source.snapshot()
        a = snapshot.private_memory(agent_id)
        if a is None:
            raise NotFound(f"{snapshot.owner_label(agent_id)} has no private memory")
        return _info(a)

    async def for_run(self, agent_id: str) -> list[MemoryBlock]:
        """组装一次运行的指令时读：这个 agent 能用的每一块（自己的与规则分来的），按名字。"""
        snapshot = await self._source.snapshot()
        grants = grants_for(snapshot, agent_id)
        return [
            MemoryBlock(a.name, _text(a), grants.writes(a), a.owner == agent_id, a.path)
            for a in usable_by(snapshot, _KIND, grants)
        ]

    async def put(self, name: str, content: str, *, version: str | None) -> MemoryInfo:
        """新建或整体改写平台的一块记忆（用户在界面上编辑）。version 是打开编辑时的版本（新建为 None），期间被别处改过
        就 Conflict（assets.check_version）。名字的规则（slug、不与别的重名）由写入口校验（asset_files.vet）。"""
        snapshot = await self._source.snapshot()
        existing = snapshot.existing(_KIND, name)
        path = existing.path if existing is not None else snapshot.path_of(_KIND, name, None)
        await self._write(path, content, version, f"memory {name!r}")
        return await self.read(name)

    async def put_private(self, agent_id: str, content: str, *, version: str | None) -> MemoryInfo:
        """整体改写 agent 的私有记忆（没有时新建），同 put。"""
        snapshot = await self._source.snapshot()
        path = snapshot.private_memory_path(agent_id)
        await self._write(path, content, version, f"the private memory of {snapshot.owner_label(agent_id)}")
        return await self.read_private(agent_id)

    async def _write(self, path: Path, content: str, version: str | None, what: str) -> None:
        def unchanged(path: Path) -> None:
            # 此刻文件里的（写入口在锁里、紧挨着写之前调用）：期间被删、被抢先建、被改过都对不上
            current = memory_file.text_of(path) if path.is_file() else None
            assets.check_version(current, version, what)

        await self._source.write({path: content}, unchanged)
