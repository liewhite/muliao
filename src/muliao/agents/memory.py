"""记忆：一份有长度上限的 markdown，每轮全文放进 instructions。存在表 memories，每次修改记一版（revisions，作者是改的人）。

两种 owner，规则相同：
- 全局记忆（owner 为 None）：所有 agent 共享，放在各 agent 自己的记忆之前；从未写过时为空（没有行）。
- agent 的记忆：读、写、看历史时正在删除的 agent 按不存在处理；创建 agent 时与它的行在同一个事务里写入（create_in），随 agent 的行删除；缺失说明数据不一致。
"""

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.active import active_row_in
from muliao.agents.kinds import Subject
from muliao.agents.revisions import RevisionInfo, history_in, record_in, revision_doc_in
from muliao.agents.tables import Memory
from muliao.errors import CorruptState, InvalidInput
from muliao.platform import text_edit
from muliao.platform.db import Database


class MemoryFull(InvalidInput):
    pass


def revision_name(owner: str | None) -> str:
    """记忆在版本表里的名字：agent 的 id，全局记忆为空串。"""
    return owner or ""


async def _row_in(s: AsyncSession, owner: str | None) -> Memory | None:
    """owner 的记忆（读与写都经这里）：正在删除的 agent 按不存在处理（NotFound）。"""
    if owner is not None:
        await active_row_in(s, owner)
    where = Memory.owner.is_(None) if owner is None else Memory.owner == owner
    row = await s.scalar(select(Memory).where(where))
    if row is None and owner is not None:
        raise _missing(owner)
    return row


def create_memory_in(s: AsyncSession, agent_id: str, author: str) -> None:
    """新建 agent 的记忆（空），在插入它的行的同一事务里（agent 领域内部，AgentService.create）。"""
    s.add(Memory(owner=agent_id, content=""))
    record_in(s, Subject.MEMORY, agent_id, None, "", author, "create memory")


class MemoryStore:
    def __init__(self, db: Database, max_chars: int):
        self._db = db
        self.max_chars = max_chars

    async def read(self, owner: str | None) -> str:
        async with self._db.transaction() as s:
            row = await _row_in(s, owner)
            return "" if row is None else row.content

    async def history(self, owner: str | None, limit: int) -> list[RevisionInfo]:
        """新的在前，第一条即当前的一版。"""
        async with self._db.transaction() as s:
            await _row_in(s, owner)
            return await history_in(s, Subject.MEMORY, revision_name(owner), limit)

    async def write(self, owner: str | None, content: str, author: str) -> str:
        """整体替换（用户在界面上编辑）。"""

        async def replace(_s: AsyncSession, _current: str) -> str:
            return content

        return await self._update(owner, replace, author)

    async def edit(self, owner: str | None, old: str, new: str, author: str) -> str:
        """规则同 skill 正文（text_edit.edit）：old 恰好出现一次时替换为 new（new 为空即删除），old 为空时把 new 追加到末尾。"""

        async def apply(_s: AsyncSession, current: str) -> str:
            return text_edit.edit(current, old, new, "memory")

        return await self._update(owner, apply, author)

    async def restore(self, owner: str | None, rev: int, author: str) -> str:
        """把第 rev 版的正文作为新的一版写入，读版本与写入在同一个事务里。"""

        async def revision(s: AsyncSession, _current: str) -> str:
            return await revision_doc_in(s, Subject.MEMORY, revision_name(owner), rev)

        return await self._update(owner, revision, author)

    async def _update(
        self, owner: str | None, edit: Callable[[AsyncSession, str], Awaitable[str]], author: str
    ) -> str:
        """读取、修改、写入与记版本在同一个写事务里：并发的修改互不覆盖。"""
        async with self._db.transaction() as s:
            row = await _row_in(s, owner)
            current = "" if row is None else row.content
            updated = await edit(s, current)
            if len(updated) > self.max_chars:
                # 给出要腾出的确切字数：只说「超了」时模型往往一次只缩短新条目几个字，反复撞墙
                over = len(updated) - self.max_chars
                raise MemoryFull(
                    f"{'global memory' if owner is None else 'your memory'} would be {len(updated)} chars, limit is {self.max_chars} "
                    f"(now {len(current)}): free at least {over} chars first by condensing or deleting "
                    f"existing entries with {'memory_edit_public' if owner is None else 'memory_edit'} "
                    "(aim for a few hundred chars of headroom), then retry"
                )
            if row is None:
                # 全局记忆从未写过：写成空串什么都没变，不建行
                if not updated:
                    return updated
                s.add(Memory(owner=None, content=updated))
            else:
                row.content = updated
            before = None if row is None else current
            record_in(s, Subject.MEMORY, revision_name(owner), before, updated, author, _label(owner))
            return updated


def _label(owner: str | None) -> str:
    return "global memory" if owner is None else f"memory of {owner}"


def _missing(owner: str) -> CorruptState:
    return CorruptState(f"memory of agent {owner!r} does not exist")
