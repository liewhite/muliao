"""条目与记忆的版本（表 revisions）：每次修改在同一个写事务里记一版（谁、何时、改前改后、说明），历史只增不改。

每种对象把自己写成一份文本文档（Document）存进 before / after：记忆是正文；skill 是全部文件的 JSON；MCP server 是
配置 JSON；secret 只有描述（不含值，也不存任何由值推出的东西）。恢复某一版 = 由对象自己的 store 把那一版的文档
作为新的一版写入（作者记为恢复的人），历史仍只增不改。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.tables import Revision
from muliao.errors import NotFound
from muliao.platform.db import Database

# 用户在界面上的修改的作者
USER = "user"
# 没有作者的系统操作（迁移导入）
SYSTEM = "muliao"


class Subject(StrEnum):
    """有版本的对象种类：条目的三种与记忆。"""

    SKILL = "skill"
    MCP = "mcp"
    SECRET = "secret"
    MEMORY = "memory"


@dataclass(frozen=True)
class RevisionInfo:
    id: int
    subject: Subject
    # 条目名；记忆是 agent 的 id，全局记忆为空串
    name: str
    before: str | None
    after: str | None
    author: str
    message: str
    at: datetime


def _info(row: Revision) -> RevisionInfo:
    return RevisionInfo(
        row.id, Subject(row.subject), row.name, row.before, row.after, row.author, row.message, row.created_at
    )


def record_in(
    s: AsyncSession,
    subject: Subject,
    name: str,
    before: str | None,
    after: str | None,
    author: str,
    message: str,
) -> None:
    """在写入对象的同一个事务里记一版；前后相同（没有变化）时不记。"""
    if before == after:
        return
    s.add(
        Revision(subject=subject.value, name=name, before=before, after=after, author=author, message=message)
    )


class Revisions:
    """版本的只读查询（界面的历史与恢复）。"""

    def __init__(self, db: Database):
        self._db = db

    async def history(self, subject: Subject, name: str, limit: int) -> list[RevisionInfo]:
        """新的在前，第一条即当前的一版（对象已删除时是那次删除）。"""
        async with self._db.transaction() as s:
            rows = await s.scalars(
                select(Revision)
                .where(Revision.subject == subject.value, Revision.name == name)
                .order_by(Revision.id.desc())
                .limit(limit)
            )
            return [_info(r) for r in rows]

    async def get(self, subject: Subject, name: str, rev: int) -> RevisionInfo:
        async with self._db.transaction() as s:
            row = await s.get(Revision, rev)
            if row is None or row.subject != subject.value or row.name != name:
                raise NotFound(f"revision {rev} of {subject.value} {name!r} not found")
            return _info(row)
