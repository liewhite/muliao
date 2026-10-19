"""prompt：agent 的指令（agents/<id>/PROMPT.md）与合并进所有 agent 的公共 prompt（public/PROMPT.md）。

存在资源仓库里（见 muliao.resources）：当前生效的就是文件内容，每次修改提交一版，历史与回滚来自 git。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from muliao.evolution import text_edit
from muliao.resources import ResourceRepo, Revision

PROMPT_FILE = "PROMPT.md"


class PromptStore:
    def __init__(self, repo: ResourceRepo):
        self._repo = repo

    def path(self, owner: str | None) -> Path:
        return self._repo.owner_dir(owner) / PROMPT_FILE

    def current(self, owner: str | None) -> str:
        """当前生效的内容。公共 prompt 从未设置过时为空（没有公共 prompt）；agent 的指令在创建时就写了，
        缺失说明数据不一致（agent 是否存在由调用方经 AgentService 确认，不在这里用目录代替）。"""
        path = self.path(owner)
        if path.exists():
            return path.read_text(encoding="utf-8")
        if owner is None:
            return ""
        raise RuntimeError(f"agent {owner} has no instructions file {path}; it must get one when created")

    async def set(self, owner: str | None, content: str, author: str) -> bool:
        """写入并提交一版；与当前内容相同时不提交，返回 False。"""
        return await self._update(owner, lambda _current: content, author)

    async def replace(self, owner: str, old: str, new: str, author: str) -> bool:
        return await self._update(
            owner, lambda current: text_edit.replace_once(current, old, new, "instructions"), author
        )

    async def append(self, owner: str, entry: str, author: str) -> bool:
        return await self._update(owner, lambda current: text_edit.append(current, entry), author)

    async def _update(self, owner: str | None, edit: Callable[[str], str], author: str) -> bool:
        """读取、修改、写入、提交在资源仓库的同一把锁里：并发的修改互不覆盖。"""
        path = self.path(owner)

        def transform(current: str | None) -> str | None:
            if current is None and owner is not None:
                raise RuntimeError(
                    f"agent {owner} has no instructions file {path}; it must get one when created"
                )
            updated = edit(current or "")
            # 空的公共 prompt 就是没有公共 prompt：不留空文件
            return None if owner is None and not updated.strip() else updated

        message = "public prompt" if owner is None else f"instructions of {owner}"
        return await self._repo.update(path, transform, message, author) is not None

    async def history(self, owner: str | None, limit: int) -> list[Revision]:
        """新的在前；第一条即当前生效的一版。"""
        return await self._repo.history(self.path(owner), limit)

    async def read_at(self, owner: str | None, rev: str) -> str:
        return await self._repo.read_at(rev, self.path(owner))

    async def restore(self, owner: str | None, rev: str, author: str) -> bool:
        """把某一版的内容作为新的一版写入（与当前相同则不写）。"""
        return await self.set(owner, await self.read_at(owner, rev), author)
