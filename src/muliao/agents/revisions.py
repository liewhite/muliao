"""版本（表 revisions）：条目、记忆、角色与 agent 的分发规则，每次修改在同一个写事务里记一版（谁、何时、改前改后、
说明），历史只增不改。有哪些对象见 agents.kinds.Subject。

每种对象把自己写成一份文本文档存进 before / after：记忆是正文；skill 是全部文件的 JSON；MCP server 是配置 JSON；
secret 只有描述（不含值，也不存任何由值推出的东西）；角色是描述与可编辑的 allow / deny；agent 是它的角色与 allow / deny。
恢复某一版 = 由对象自己的 store 在一个写事务里读出那一版（revision_doc_in）、把它的文档作为新的一版写入（改的人记为恢复的
人），历史仍只增不改。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.kinds import Subject, label
from muliao.agents.tables import Revision
from muliao.errors import InvalidInput, NotFound

# 没有改的人的系统操作（迁移导入）
SYSTEM = "muliao"

# 能恢复旧版的对象：secret 的版本不含值，角色与 agent 规则的版本只供查看
RESTORABLE = frozenset({Subject.MEMORY, Subject.SKILL, Subject.MCP})


@dataclass(frozen=True)
class RevisionInfo:
    id: int
    subject: Subject
    # 条目名、角色名；记忆与 agent 是 agent 的 id（全局记忆为空串）
    name: str
    before: str | None
    after: str | None
    actor: str
    message: str
    at: datetime

    @property
    def restorable(self) -> bool:
        """能恢复到这一版：对象的种类能恢复，而且这一版不是删除（revision_doc_in 同一条规则）。"""
        return self.subject in RESTORABLE and self.after is not None


def _info(row: Revision) -> RevisionInfo:
    return RevisionInfo(
        row.id, Subject(row.subject), row.name, row.before, row.after, row.actor, row.message, row.created_at
    )


def described(reason: str | None, default: str) -> str:
    """一版的说明：改的人给了原因（agent 用工具改记忆、skill 时必给：它针对的缺陷或重复模式、来源、与同一条以前改动的
    关系，构成自进化的时间线）就记原因，否则（界面、系统操作）记默认的动作描述。给了却是空白是错误。"""
    if reason is None:
        return default
    if not reason.strip():
        raise InvalidInput("reason must not be blank: say why you make this change")
    return reason.strip()


def record_in(
    s: AsyncSession,
    subject: Subject,
    name: str,
    before: str | None,
    after: str | None,
    actor: str,
    message: str,
    *,
    force: bool = False,
) -> None:
    """在写入对象的同一个事务里记一版；前后相同（没有变化）时不记。force：文档不反映的修改也记一版，由
    message 说明（换 owner：文档不含 owner；secret 改值：文档不含值）。"""
    if before == after and not force:
        return
    s.add(
        Revision(subject=subject.value, name=name, before=before, after=after, actor=actor, message=message)
    )


async def revision_doc_in(s: AsyncSession, subject: Subject, name: str, rev: int) -> str:
    """要恢复的那一版之后的文档（在恢复的写事务里读）。不是这个对象的版本 NotFound；那一版是删除时 InvalidInput。"""
    row = await s.get(Revision, rev)
    if row is None or row.subject != subject.value or row.name != name:
        raise NotFound(f"revision {rev} of {label(subject)} {name!r} not found")
    if subject not in RESTORABLE:
        raise InvalidInput(f"{label(subject)} revisions cannot be restored")
    if row.after is None:
        raise InvalidInput(f"revision {rev} is a deletion; there is nothing to restore")
    return row.after


async def history_in(s: AsyncSession, subject: Subject, name: str, limit: int) -> list[RevisionInfo]:
    """新的在前，第一条即当前的一版（对象已删除时是那次删除）。"""
    rows = await s.scalars(
        select(Revision)
        .where(Revision.subject == subject.value, Revision.name == name)
        .order_by(Revision.id.desc())
        .limit(limit)
    )
    return [_info(r) for r in rows]
