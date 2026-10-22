"""Agent 私有记忆：一份有长度上限的 markdown，每轮全文注入 instructions。存在资源仓库里，每次修改提交一版。"""

from collections.abc import Callable
from pathlib import Path

from muliao.agents.resources import ResourceRepo
from muliao.errors import CorruptState, InvalidInput
from muliao.platform import text_edit

MEMORY_FILE = "MEMORY.md"


class MemoryFull(InvalidInput):
    pass


class MemoryStore:
    def __init__(self, repo: ResourceRepo, max_chars: int):
        # 写记忆之前 agent 还可用的检查由仓库做（ResourceRepo）
        self._repo = repo
        self.max_chars = max_chars

    def path(self, agent: str) -> Path:
        return self._repo.owner_dir(agent) / MEMORY_FILE

    def read(self, agent: str) -> str:
        path = self.path(agent)
        if not path.exists():
            # 创建 agent 时就写了记忆文件
            raise CorruptState(f"memory file for agent {agent!r} does not exist: {path}")
        return path.read_text(encoding="utf-8")

    async def write(self, agent: str, content: str, author: str) -> str:
        """整体替换（用户在界面上编辑时）。"""
        return await self._update(agent, lambda _current: content, author)

    async def append(self, agent: str, entry: str, author: str) -> str:
        return await self._update(agent, lambda current: text_edit.append(current, entry), author)

    async def replace(self, agent: str, old: str, new: str, author: str) -> str:
        """把恰好出现一次的 old 替换为 new；new 为空即删除。"""
        return await self._update(
            agent, lambda current: text_edit.replace_once(current, old, new, "memory"), author
        )

    async def _update(self, agent: str, edit: Callable[[str], str], author: str) -> str:
        """读取、修改、写入、提交在资源仓库的同一把锁里：并发的修改互不覆盖。"""
        path = self.path(agent)
        result: list[str] = []

        def transform(current: str | None) -> str:
            if current is None:
                raise CorruptState(f"memory file for agent {agent!r} does not exist: {path}")
            updated = edit(current)
            if len(updated) > self.max_chars:
                # 给出要腾出的确切字数：只说「超了」时模型往往一次只缩短新条目几个字，反复撞墙
                over = len(updated) - self.max_chars
                raise MemoryFull(
                    f"memory would be {len(updated)} chars, limit is {self.max_chars} (now {len(current)}): "
                    f"free at least {over} chars first by condensing or deleting existing entries with "
                    "memory_replace (aim for a few hundred chars of headroom), then retry"
                )
            result.append(updated)
            return updated

        await self._repo.update(path, transform, f"memory of {agent}", author)
        return result[0]
