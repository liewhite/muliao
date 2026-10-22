"""记忆：一份有长度上限的 markdown，每轮全文放进 instructions。存在资源仓库里，每次修改提交一版（作者是改的人）。

两种 owner，规则相同：
- 全局记忆（owner 为 None，public/MEMORY.md）：所有 agent 共享，放在各 agent 自己的记忆之前；从未写过时为空。
- agent 的记忆（agents/<id>/MEMORY.md）：创建 agent 时就写了，缺失说明数据不一致。
"""

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
        # 写 agent 的记忆之前它还可用的检查由仓库做（ResourceRepo）
        self._repo = repo
        self.max_chars = max_chars

    def path(self, owner: str | None) -> Path:
        return self._repo.owner_dir(owner) / MEMORY_FILE

    def read(self, owner: str | None) -> str:
        path = self.path(owner)
        if path.exists():
            return path.read_text(encoding="utf-8")
        if owner is None:
            return ""
        raise _missing(owner, path)

    async def write(self, owner: str | None, content: str, author: str) -> str:
        """整体替换（用户在界面上编辑、恢复旧版时）。"""
        return await self._update(owner, lambda _current: content, author)

    async def append(self, owner: str | None, entry: str, author: str) -> str:
        return await self._update(owner, lambda current: text_edit.append(current, entry), author)

    async def replace(self, owner: str | None, old: str, new: str, author: str) -> str:
        """把恰好出现一次的 old 替换为 new；new 为空即删除。"""
        return await self._update(
            owner, lambda current: text_edit.replace_once(current, old, new, "memory"), author
        )

    async def _update(self, owner: str | None, edit: Callable[[str], str], author: str) -> str:
        """读取、修改、写入、提交在资源仓库的同一把锁里：并发的修改互不覆盖。"""
        path = self.path(owner)
        result: list[str] = []

        def transform(current: str | None) -> str | None:
            if current is None and owner is not None:
                raise _missing(owner, path)
            updated = edit(current or "")
            if len(updated) > self.max_chars:
                # 给出要腾出的确切字数：只说「超了」时模型往往一次只缩短新条目几个字，反复撞墙
                over = len(updated) - self.max_chars
                raise MemoryFull(
                    f"{_label(owner)} would be {len(updated)} chars, limit is {self.max_chars} "
                    f"(now {len(current or '')}): free at least {over} chars first by condensing or deleting "
                    "existing entries with memory_replace (aim for a few hundred chars of headroom), then retry"
                )
            result.append(updated)
            # 全局记忆从未写过、仍为空：什么都没变，不提交（None 删除一个不存在的路径即无变化）
            if current is None and not updated:
                return None
            return updated

        await self._repo.update(path, transform, _label(owner), author)
        return result[0]


def _label(owner: str | None) -> str:
    # 也是提交信息（资源仓库的历史里按它区分）
    return "global memory" if owner is None else f"memory of {owner}"


def _missing(owner: str, path: Path) -> CorruptState:
    return CorruptState(f"memory file for agent {owner!r} does not exist: {path}")
