"""prompt：agent 的指令（agents/<id>/PROMPT.md）与合并进所有 agent 的公共 prompt（public/PROMPT.md）。

存在资源仓库里（见 muliao.agents.resources）：当前生效的就是文件内容，每次修改提交一版，历史与回滚来自 git。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

from muliao.agents.active import OwnerCheck
from muliao.agents.resources import ResourceRepo, Revision
from muliao.errors import CorruptState
from muliao.platform import text_edit

PROMPT_FILE = "PROMPT.md"


class PromptStore:
    def __init__(self, repo: ResourceRepo, check_owner: OwnerCheck):
        self._repo = repo
        # 写 agent 的指令之前在仓库的锁里检查它还可用（agents.active）
        self._check_owner = check_owner

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
        raise _missing(owner, path)

    async def set(self, owner: str | None, content: str, author: str) -> bool:
        """写入并提交一版；与当前内容相同时不提交，返回 False。"""
        return await self._update(owner, lambda _current: content, author)

    async def edit(self, owner: str, old: str, new: str, author: str) -> bool:
        """改 agent 自己的指令（规则见 text_edit.edit）。"""
        return await self._update(
            owner, lambda current: text_edit.edit(current, old, new, "instructions"), author
        )

    async def _update(self, owner: str | None, edit: Callable[[str], str], author: str) -> bool:
        """读取、修改、写入、提交在资源仓库的同一把锁里：并发的修改互不覆盖。"""
        path = self.path(owner)

        def transform(current: str | None) -> str | None:
            if current is None and owner is not None:
                raise _missing(owner, path)
            updated = edit(current or "")
            # 从未设置过、仍为空：什么都没变，不提交（None 删除一个不存在的路径即无变化）
            if current is None and not updated:
                return None
            # 清空也写成一版（空文件）：文件只在从未设置过时缺失，历史中间不会出现「不存在」的版本
            return updated

        message = "public prompt" if owner is None else f"instructions of {owner}"
        updated = await self._repo.update(
            path, transform, message, author, precondition=partial(self._check_owner, owner)
        )
        return updated is not None

    async def history(self, owner: str | None, limit: int) -> list[Revision]:
        """新的在前；第一条即当前生效的一版。"""
        return await self._repo.history(self.path(owner), limit)

    async def read_at(self, owner: str | None, rev: str) -> str:
        return await self._repo.read_at(rev, self.path(owner))

    async def restore(self, owner: str | None, rev: str, author: str) -> bool:
        """把某一版的内容作为新的一版写入（与当前相同则不写）。"""
        return await self.set(owner, await self.read_at(owner, rev), author)


def _missing(owner: str, path: Path) -> CorruptState:
    """agent 的指令文件缺失：创建时就写了，缺失说明数据不一致。"""
    return CorruptState(f"agent {owner} has no instructions file {path}; it must get one when created")
