"""记忆：一份有长度上限的 markdown，每轮全文放进 instructions。存在表 memories，每次修改记一版（revisions，记改的人 actor）。

两种记忆，规则相同（按 agent_id 区分：某个 agent 的 id，或 None 指全局记忆；不叫 owner，owner 只指条目的归属）：
- 全局记忆（agent_id 为 None）：所有 agent 共享，放在各 agent 自己的记忆之前；从未写过时为空（没有行）。
- agent 的记忆：读、写、看历史时正在删除的 agent 按不存在处理；创建 agent 时与它的行在同一个事务里写入（create_in），随 agent 的行删除；缺失说明数据不一致。
"""

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.active import check_active_in
from muliao.agents.kinds import Subject
from muliao.agents.revisions import RevisionInfo, history_in, record_in, revision_doc_in
from muliao.agents.tables import Memory
from muliao.errors import CorruptState, InvalidInput
from muliao.platform import text_edit
from muliao.platform.db import Database


class MemoryFull(InvalidInput):
    pass


def revision_name(agent_id: str | None) -> str:
    """记忆在版本表里的名字：agent 的 id，全局记忆为空串。"""
    return agent_id or ""


async def _row_in(s: AsyncSession, agent_id: str | None) -> Memory | None:
    """agent_id 的记忆（用户与工具的读、看历史、写都经这里）：正在删除的 agent 按不存在处理（NotFound）。"""
    if agent_id is not None:
        await check_active_in(s, agent_id)
    return await _stored_in(s, agent_id)


async def _stored_in(s: AsyncSession, agent_id: str | None) -> Memory | None:
    where = Memory.agent_id.is_(None) if agent_id is None else Memory.agent_id == agent_id
    row = await s.scalar(select(Memory).where(where))
    if row is None and agent_id is not None:
        raise _missing(agent_id)
    return row


def create_memory_in(s: AsyncSession, agent_id: str, actor: str) -> None:
    """新建 agent 的记忆（空），在插入它的行的同一事务里（agent 领域内部，AgentService.create）。"""
    s.add(Memory(agent_id=agent_id, content=""))
    record_in(s, Subject.MEMORY, agent_id, None, "", actor, "create memory")


async def record_memory_deleted_in(s: AsyncSession, agent_id: str, actor: str, message: str) -> None:
    """agent 删除时为它的记忆记一版删除（行随 agent 的行级联删除；版本表留作审计），在删除流程的写事务里。"""
    row = await s.scalar(select(Memory).where(Memory.agent_id == agent_id))
    if row is None:
        raise _missing(agent_id)
    record_in(s, Subject.MEMORY, revision_name(agent_id), row.content, None, actor, message)


class MemoryStore:
    def __init__(self, db: Database, max_chars: int):
        self._db = db
        self.max_chars = max_chars

    async def read(self, agent_id: str | None) -> str:
        async with self._db.transaction() as s:
            row = await _row_in(s, agent_id)
            return "" if row is None else row.content

    async def for_run(self, agent_id: str | None) -> str:
        """组装一次运行的指令时读：包括正在删除的 agent。删除标记之后仍可能有它的运行开始执行（标记与删除前的检查分属
        两个事务），它们由删除流程停下，不该因为读不到记忆而失败（与 sessions.loading 读 agent 同一口径）。"""
        async with self._db.transaction() as s:
            row = await _stored_in(s, agent_id)
            return "" if row is None else row.content

    async def history(self, agent_id: str | None, limit: int) -> list[RevisionInfo]:
        """新的在前，第一条即当前的一版。"""
        async with self._db.transaction() as s:
            await _row_in(s, agent_id)
            return await history_in(s, Subject.MEMORY, revision_name(agent_id), limit)

    async def write(self, agent_id: str | None, content: str, actor: str) -> str:
        """整体替换（用户在界面上编辑）。"""

        async def replace(_s: AsyncSession, _current: str) -> str:
            return content

        return await self._update(agent_id, replace, actor)

    async def edit(self, agent_id: str | None, old: str, new: str, actor: str) -> str:
        """规则同 skill 正文（text_edit.edit）：old 恰好出现一次时替换为 new（new 为空即删除），old 为空时把 new 追加到末尾。"""

        async def apply(_s: AsyncSession, current: str) -> str:
            return text_edit.edit(current, old, new, "memory")

        return await self._update(agent_id, apply, actor)

    async def restore(self, agent_id: str | None, rev: int, actor: str) -> str:
        """把第 rev 版的正文作为新的一版写入，读版本与写入在同一个事务里。"""

        async def revision(s: AsyncSession, _current: str) -> str:
            return await revision_doc_in(s, Subject.MEMORY, revision_name(agent_id), rev)

        return await self._update(agent_id, revision, actor)

    async def _update(
        self, agent_id: str | None, edit: Callable[[AsyncSession, str], Awaitable[str]], actor: str
    ) -> str:
        """读取、修改、写入与记版本在同一个写事务里：并发的修改互不覆盖。"""
        async with self._db.transaction() as s:
            row = await _row_in(s, agent_id)
            current = "" if row is None else row.content
            updated = await edit(s, current)
            if len(updated) > self.max_chars:
                # 给出要腾出的确切字数：只说「超了」时模型往往一次只缩短新条目几个字，反复撞墙
                over = len(updated) - self.max_chars
                raise MemoryFull(
                    f"{'global memory' if agent_id is None else 'your memory'} would be {len(updated)} chars, limit is {self.max_chars} "
                    f"(now {len(current)}): free at least {over} chars first by condensing or deleting "
                    f"existing entries with {'memory_edit_public' if agent_id is None else 'memory_edit'} "
                    "(aim for a few hundred chars of headroom), then retry"
                )
            if row is None:
                # 全局记忆从未写过：写成空串什么都没变，不建行
                if not updated:
                    return updated
                s.add(Memory(agent_id=None, content=updated))
            else:
                row.content = updated
            before = None if row is None else current
            record_in(s, Subject.MEMORY, revision_name(agent_id), before, updated, actor, _label(agent_id))
            return updated


def _label(agent_id: str | None) -> str:
    return "global memory" if agent_id is None else f"memory of {agent_id}"


def _missing(agent_id: str) -> CorruptState:
    return CorruptState(f"memory of agent {agent_id!r} does not exist")
