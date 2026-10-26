"""条目（表 items）：skill、MCP server、secret 共用的存取规则。各种类的 store（skills、mcp、secrets）只给出自己的内容。

- 名字按种类全局唯一（没有同名遮蔽）。
- 改、删已有条目按规则 2（agents.access.check_change_in）：用户、owner 或 admin。
- 新建条目的 owner 由调用方给出（agent 建的归它自己）；为 agent 新建时在同一个写事务里检查它还可用（正在删除的按不存在
  处理，agents.active）。
- 每次修改与版本（revisions）在同一个事务里记。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.access import check_change_in, grants_in
from muliao.agents.active import active_row_in
from muliao.agents.permissions import Kind
from muliao.agents.revisions import Subject
from muliao.agents.tables import Item
from muliao.errors import NotFound


async def item_in(s: AsyncSession, kind: Subject, name: str) -> Item | None:
    return await s.scalar(select(Item).where(Item.kind == kind.value, Item.name == name))


async def existing_in(s: AsyncSession, kind: Subject, name: str) -> Item:
    row = await item_in(s, kind, name)
    if row is None:
        raise NotFound(f"{label(kind)} {name!r} not found")
    return row


async def changeable_in(s: AsyncSession, kind: Subject, name: str, actor: str) -> Item:
    """要改或删的已有条目：存在，actor 能改它（规则 2），它的 owner（agent）还可用（正在删除的 agent 的条目按不存在处理，
    由删除流程统一处理）。"""
    row = await existing_in(s, kind, name)
    await _check_owner_active_in(s, kind, row)
    await check_change_in(s, actor, row)
    return row


async def _check_owner_active_in(s: AsyncSession, kind: Subject, row: Item) -> None:
    if row.owner is not None:
        try:
            await active_row_in(s, row.owner)
        except NotFound:
            raise NotFound(f"{label(kind)} {row.name!r} not found") from None


async def writable_in(
    s: AsyncSession, kind: Subject, name: str, actor: str, owner: str | None
) -> Item | None:
    """新建或整体改写之前：已存在就按规则 2 检查并返回它（owner 不变）；不存在时返回 None，新条目将归 owner（agent 时
    检查它还可用）。"""
    row = await item_in(s, kind, name)
    if row is not None:
        await _check_owner_active_in(s, kind, row)
        await check_change_in(s, actor, row)
        return row
    if owner is not None:
        await active_row_in(s, owner)
    return None


async def items_in(s: AsyncSession, kind: Subject) -> list[Item]:
    """这一种的全部条目，按名称。"""
    return list(await s.scalars(select(Item).where(Item.kind == kind.value).order_by(Item.name)))


async def usable_in(s: AsyncSession, kind: Subject, user: str) -> list[Item]:
    """user（agent 的 id 或 revisions.USER）能用的（规则 1），按名称。"""
    grants = await grants_in(s, user)
    return [r for r in await items_in(s, kind) if grants.uses(Kind(r.kind), r.name, r.owner)]


def label(kind: Subject) -> str:
    return {Subject.SKILL: "skill", Subject.MCP: "MCP server", Subject.SECRET: "secret"}[kind]
