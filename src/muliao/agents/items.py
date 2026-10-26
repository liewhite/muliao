"""条目（表 items）：skill、MCP server、secret 共用的存取规则。各种类的 store（skills、mcp、secrets）只给出自己的内容。

- 名字按种类全局唯一：写入时同名条目属于别的 owner 就是冲突（没有同名遮蔽）。
- 为 agent 写条目，在同一个写事务里检查它还可用（正在删除的按不存在处理，agents.active）。
- 每次修改与版本（revisions）在同一个事务里记。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.active import active_row_in
from muliao.agents.ownership import scope_of
from muliao.agents.revisions import Subject
from muliao.agents.tables import Item
from muliao.errors import Conflict, NotFound


async def item_in(s: AsyncSession, kind: Subject, name: str) -> Item | None:
    return await s.scalar(select(Item).where(Item.kind == kind.value, Item.name == name))


async def owned_item_in(
    s: AsyncSession, kind: Subject, owner: str | None, name: str, *, write: bool = False
) -> Item:
    """owner 名下的这一项；不存在或属于别人时 NotFound（按 owner 的范围说）。write：要改它（owner 必须还可用）。"""
    if write and owner is not None:
        await active_row_in(s, owner)
    row = await item_in(s, kind, name)
    if row is None or row.owner != owner:
        raise NotFound(f"{scope_of(owner)} {label(kind)} {name!r} not found")
    return row


async def writable_in(s: AsyncSession, kind: Subject, owner: str | None, name: str) -> Item | None:
    """为 owner 写这一项之前：owner（agent）还可用；同名条目若存在必须也归它（返回它），否则冲突。"""
    if owner is not None:
        await active_row_in(s, owner)
    row = await item_in(s, kind, name)
    if row is not None and row.owner != owner:
        raise Conflict(
            f"a {scope_of(row.owner)} {label(kind)} named {name!r} already exists; names are unique across "
            f"public and private, choose another name"
        )
    return row


async def items_in(s: AsyncSession, kind: Subject, *where: object) -> list[Item]:
    query = select(Item).where(Item.kind == kind.value, *where)  # type: ignore[arg-type]
    return list(await s.scalars(query.order_by(Item.owner.is_(None), Item.name)))


def label(kind: Subject) -> str:
    return {Subject.SKILL: "skill", Subject.MCP: "MCP server", Subject.SECRET: "secret"}[kind]
