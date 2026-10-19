"""Agent 私有记忆：一份有长度上限的 markdown，每轮全文注入 instructions。存在资源仓库里，每次修改提交一版。"""

from pathlib import Path

from muliao.errors import InvalidInput, NotFound
from muliao.resources import ResourceRepo

MEMORY_FILE = "MEMORY.md"


class MemoryFull(InvalidInput):
    pass


class MemoryStore:
    def __init__(self, repo: ResourceRepo, max_chars: int):
        self._repo = repo
        self.max_chars = max_chars

    def path(self, agent: str) -> Path:
        return self._repo.owner_dir(agent) / MEMORY_FILE

    def init(self, agent: str) -> Path:
        """新建 agent 时写入空记忆；由调用方与其他初始文件一起提交。"""
        path = self.path(agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")
        return path

    def read(self, agent: str) -> str:
        path = self.path(agent)
        if not path.exists():
            raise NotFound(f"memory file for agent {agent!r} does not exist: {path}")
        return path.read_text(encoding="utf-8")

    async def write(self, agent: str, content: str, author: str) -> str:
        if len(content) > self.max_chars:
            raise MemoryFull(
                f"memory would be {len(content)} chars, limit is {self.max_chars}; "
                "condense or remove existing entries first"
            )
        path = self.path(agent)
        if not path.exists():
            raise NotFound(f"memory file for agent {agent!r} does not exist: {path}")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        await self._repo.commit([path], f"memory of {agent}", author)
        return content

    async def append(self, agent: str, entry: str, author: str) -> str:
        entry = entry.strip()
        if not entry:
            raise InvalidInput("memory entry must not be empty")
        current = self.read(agent).rstrip()
        return await self.write(agent, f"{current}\n{entry}\n" if current else f"{entry}\n", author)

    async def replace(self, agent: str, old: str, new: str, author: str) -> str:
        """把恰好出现一次的 old 替换为 new；new 为空即删除。"""
        current = self.read(agent)
        count = current.count(old) if old else 0
        if count != 1:
            raise InvalidInput(f"old text must occur exactly once in memory, found {count} occurrences")
        return await self.write(agent, current.replace(old, new), author)
