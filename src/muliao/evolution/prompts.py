"""prompt 的版本化存储：agent 的指令（owner = agent id）与合并进所有 agent 的公共 prompt（owner = None）。

当前生效的就是最新一版；修改即追加一版，回滚即把旧版内容作为新的一版写入，所以历史只增不改。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.errors import NotFound
from muliao.models import Agent, PromptRevision
from muliao.ownership import owned_by

log = logging.getLogger(__name__)

# 修改者为用户时记录的 author；agent 修改时记录它的 id
USER = "user"
HISTORY_LIMIT = 50


class PromptStore:
    """prompt 读写的唯一入口：「当前 = 最新一版」只在 _latest 实现一次。"""

    def __init__(self, db: Database):
        self._db = db

    async def current_in(self, s: AsyncSession, owner: str | None) -> str:
        """当前生效的内容。公共 prompt 从未设置过时为空（没有公共 prompt）；agent 的指令在创建时就写了第一版。"""
        content = await s.scalar(_latest(owner))
        if content is not None:
            return content
        if owner is None:
            return ""
        if await s.get(Agent, owner) is None:
            # 读到一半 agent 被删除（版本随之级联删除）：如实报告，不当作数据损坏
            raise NotFound(f"agent {owner!r} not found")
        raise RuntimeError(f"agent {owner} has no instructions revision; it must get one when created")

    async def current(self, owner: str | None) -> str:
        async with self._db.transaction() as s:
            return await self.current_in(s, owner)

    async def current_many(self, owners: list[str]) -> dict[str, str]:
        """多个 agent 的当前指令（agent 列表用）。"""
        async with self._db.transaction() as s:
            return {owner: await self.current_in(s, owner) for owner in owners}

    async def set_in(
        self, s: AsyncSession, owner: str | None, content: str, author: str
    ) -> PromptRevision | None:
        """写入新的一版；与当前内容相同时不写，返回 None。agent 指令变化时同时更新 agent 的 updated_at。"""
        agent = None if owner is None else await s.get(Agent, owner)
        if owner is not None and agent is None:
            raise NotFound(f"agent {owner!r} not found")
        if content == await self.current_in(s, owner):
            return None
        revision = PromptRevision(owner=owner, content=content, author=author)
        s.add(revision)
        if agent is not None:
            agent.updated_at = utcnow()
        await s.flush()
        log.info("prompt revised", extra={"owner": owner, "revision": revision.id, "author": author})
        return revision

    async def set(self, owner: str | None, content: str, author: str) -> PromptRevision | None:
        async with self._db.transaction() as s:
            return await self.set_in(s, owner, content, author)

    async def history(self, owner: str | None) -> list[PromptRevision]:
        """最近 HISTORY_LIMIT 个版本，新的在前；更早的版本保留在库里，但不在这里列出。"""
        async with self._db.transaction() as s:
            query = (
                select(PromptRevision)
                .where(owned_by(PromptRevision.owner, owner))
                .order_by(PromptRevision.id.desc())
                .limit(HISTORY_LIMIT)
            )
            return list((await s.scalars(query)).all())

    async def restore(self, revision_id: int, author: str) -> PromptRevision | None:
        """把某一版的内容作为新的一版写入（与当前相同则不写）。"""
        async with self._db.transaction() as s:
            old = await s.get(PromptRevision, revision_id)
            if old is None:
                raise NotFound(f"prompt revision {revision_id} not found")
            return await self.set_in(s, old.owner, old.content, author)


def _latest(owner: str | None):  # noqa: ANN202
    # AUTOINCREMENT 保证 id 单调：id 最大的就是最新一版（不按时间排序，避免时钟回拨与同一时刻写入）
    return (
        select(PromptRevision.content)
        .where(owned_by(PromptRevision.owner, owner))
        .order_by(PromptRevision.id.desc())
        .limit(1)
    )
