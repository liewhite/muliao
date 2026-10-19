"""prompt：agent 的指令（agents/<id>/PROMPT.md）与合并进所有 agent 的公共 prompt（public/PROMPT.md）。

存在资源仓库里（见 muliao.resources）：当前生效的就是文件内容，每次修改提交一版，历史与回滚来自 git。
"""

from __future__ import annotations

from pathlib import Path

from muliao.errors import NotFound
from muliao.resources import ResourceRepo, Revision

PROMPT_FILE = "PROMPT.md"


class PromptStore:
    def __init__(self, repo: ResourceRepo):
        self._repo = repo

    def path(self, owner: str | None) -> Path:
        return self._repo.owner_dir(owner) / PROMPT_FILE

    def init(self, agent_id: str, content: str) -> Path:
        """新建 agent 时写入第一版指令；由调用方与其他初始文件一起提交。"""
        path = self.path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def current(self, owner: str | None) -> str:
        """当前生效的内容。公共 prompt 从未设置过时为空（没有公共 prompt）；agent 的指令在创建时就写了。"""
        path = self.path(owner)
        if path.exists():
            return path.read_text(encoding="utf-8")
        if owner is None:
            return ""
        if not self._repo.owner_dir(owner).exists():
            raise NotFound(f"agent {owner!r} not found")
        raise RuntimeError(f"agent {owner} has no {PROMPT_FILE}; it must get one when created")

    async def set(self, owner: str | None, content: str, author: str) -> bool:
        """写入并提交一版；与当前内容相同时不写，返回 False。"""
        if content == self.current(owner):
            return False
        path = self.path(owner)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        await self._repo.commit(
            [path], f"{'public prompt' if owner is None else f'instructions of {owner}'}", author
        )
        return True

    async def history(self, owner: str | None, limit: int) -> list[Revision]:
        """新的在前；第一条即当前生效的一版。"""
        return await self._repo.history(self.path(owner), limit)

    async def read_at(self, owner: str | None, rev: str) -> str:
        return await self._repo.read_at(rev, self.path(owner))

    async def restore(self, owner: str | None, rev: str, author: str) -> bool:
        """把某一版的内容作为新的一版写入（与当前相同则不写）。"""
        return await self.set(owner, await self.read_at(owner, rev), author)
