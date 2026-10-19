"""prompt 的版本化存储：agent 的指令（owner = agent id）与合并进所有 agent 的公共 prompt（owner = None）。

当前生效的就是最新一版；修改即追加一版，回滚即把旧版内容作为新的一版写入，所以历史只增不改。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.errors import NotFound
from muliao.models import Agent, PromptRevision

log = logging.getLogger(__name__)

# 修改者为用户时记录的 author；agent 修改时记录它的 id
USER = "user"
HISTORY_LIMIT = 50


class PromptStore:
    def __init__(self, db: Database):
        self._db = db

    async def current_in(self, s: AsyncSession, owner: str | None) -> str | None:
        """最新一版的内容；从未设置过时为 None（公共 prompt 可以不存在，agent 创建时一定会写第一版）。"""
        return await s.scalar(
            select(PromptRevision.content).where(_owned_by(owner)).order_by(PromptRevision.id.desc()).limit(1)
        )

    async def public(self) -> str:
        """公共 prompt；从未设置时为空，即没有公共 prompt。"""
        async with self._db.transaction() as s:
            return await self.current_in(s, None) or ""

    async def set_in(
        self, s: AsyncSession, owner: str | None, content: str, author: str
    ) -> PromptRevision | None:
        """写入新的一版；与当前内容相同时不写，返回 None。"""
        if owner is not None and await s.get(Agent, owner) is None:
            raise NotFound(f"agent {owner!r} not found")
        if content == await self.current_in(s, owner):
            return None
        revision = PromptRevision(owner=owner, content=content, author=author)
        s.add(revision)
        await s.flush()
        log.info("prompt revised", extra={"owner": owner, "revision": revision.id, "author": author})
        return revision

    async def set(self, owner: str | None, content: str, author: str) -> PromptRevision | None:
        async with self._db.transaction() as s:
            return await self.set_in(s, owner, content, author)

    async def history(self, owner: str | None) -> list[PromptRevision]:
        """最近的版本，新的在前。"""
        async with self._db.transaction() as s:
            query = (
                select(PromptRevision)
                .where(_owned_by(owner))
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


def _owned_by(owner: str | None):  # noqa: ANN202
    return PromptRevision.owner.is_(None) if owner is None else PromptRevision.owner == owner
