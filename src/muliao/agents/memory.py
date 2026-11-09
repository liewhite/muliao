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

from muliao.agents import assets
from muliao.agents.access import changeable, grants_for, usable_by, writable
from muliao.agents.kinds import Kind, StoredKind, label
from muliao.agents.naming import validate_slug
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


def _info(a: assets.Asset) -> MemoryInfo:
    return MemoryInfo(a.name, a.owner, a.text, a.updated_at, a.path)


class MemoryStore:
    def __init__(self, source: AssetSource, max_chars: int):
        self._source = source
        # 每一块各自的上限
        self.max_chars = max_chars

    async def read(self, name: str) -> MemoryInfo:
        return _info((await self._source.snapshot()).asset(_KIND, name))

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

    async def put(self, name: str, content: str, *, actor: str, owner: str | None = None) -> MemoryInfo:
        """新建或整体改写（用户在界面上编辑）：已有的要 actor 能改（Grants.can 的 write），owner 不变；新建的归 owner。"""
        async with self._source.writing() as snapshot:
            existing = writable(snapshot, _KIND, name, actor)
            path = existing.path if existing is not None else self._new_path(snapshot, name, owner)
            await asyncio.to_thread(assets.write_text, path, _file_text(self._tidied(name, content)))
        return await self.read(name)

    def _new_path(self, snapshot: assets.Snapshot, name: str, owner: str | None) -> Path:
        if owner is None:
            validate_slug(_LABEL, name)
        else:
            agent = snapshot.agents.get(owner)
            if agent is None or agent.name != name:
                raise InvalidInput(
                    f"an agent's private memory is named after the agent ({agent.name if agent else owner!r}); "
                    "other memories belong to the platform"
                )
        return snapshot.path_of(_KIND, name, owner)

    async def delete(self, name: str, actor: str) -> None:
        """全局记忆不能删（base 固定收录它）。"""
        if name == GLOBAL_MEMORY:
            raise InvalidInput(f"the global memory {GLOBAL_MEMORY!r} cannot be deleted")
        async with self._source.writing() as snapshot:
            asset = changeable(snapshot, _KIND, name, actor)
            await asyncio.to_thread(assets.remove, asset.path)

    def _tidied(self, name: str, content: str) -> str:
        """每条写入存之前都经过这里：规整空白（tidy），再按规整后的长度查上限。"""
        content = tidy(content)
        if len(content) > self.max_chars:
            raise InvalidInput(f"memory {name!r} would be {len(content)} chars, limit is {self.max_chars}")
        return content


def tidy(content: str) -> str:
    """记忆写入前规整空白（每条写入都经过这里）：每行去掉行尾空白，连续空行合并成一个，去掉首尾空行，不留结尾换行，存下的
    就是指令里显示的样子。只动空白：记忆一条一行，空行个数和行尾空格没有意义，而指令里看不出它们（#361）。"""
    lines: list[str] = []
    for line in content.splitlines():
        line = line.rstrip()
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _file_text(content: str) -> str:
    """存进文件的样子：规整后的正文加结尾换行（读树时去掉首尾空白，指令里显示的就是规整后的正文）。"""
    return content + "\n" if content else ""
