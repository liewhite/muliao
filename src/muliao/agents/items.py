"""条目（表 items，条目子域）：skill、MCP server、secret 共用的存取规则。各种类的 store（skills、mcp、secrets）只给出自己的内容；
owner 的变更（转移、随 agent 删除）在 agents.ownership；谁能用、谁能改是分发子域的规则（agents.access 的 changeable_in、
writable_in、usable_in 在这里的查询上加上规则）。

- 名字按种类全局唯一（没有同名遮蔽）。
- 正在删除的 agent 的条目按不存在处理：读（existing_in、check_visible_in、visible_in）与写一样。
- 新建条目的 owner 由调用方给出（agent 建的归它自己）；为 agent 新建时在同一个写事务里检查它还可用（正在删除的按不存在
  处理，agents.active）。
- 每次修改与版本（revisions）在同一个事务里记。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.active import active_row_in, agent_name_in, deleting_ids
from muliao.agents.kinds import Kind, StoredKind, label
from muliao.agents.permissions import pattern_of
from muliao.agents.tables import Item
from muliao.errors import Conflict, NotFound


async def item_in(s: AsyncSession, kind: StoredKind, name: str) -> Item | None:
    return await s.scalar(select(Item).where(Item.kind == kind.value, Item.name == name))


async def existing_in(s: AsyncSession, kind: StoredKind, name: str) -> Item:
    """要读、改或删的已有条目：存在，它的 owner（agent）还可用——正在删除的 agent 的条目按不存在处理（读与写一样），
    由删除流程统一处理。"""
    row = await item_in(s, kind, name)
    if row is None:
        raise NotFound(f"{label(kind)} {name!r} not found")
    await check_owner_active_in(s, kind, row)
    return row


async def check_visible_in(s: AsyncSession, kind: StoredKind, name: str) -> None:
    """看版本历史之前：条目还在时同 existing_in（正在删除的 agent 的按不存在处理）；已经删掉的照样看得到它的历史。"""
    row = await item_in(s, kind, name)
    if row is not None:
        await check_owner_active_in(s, kind, row)


async def check_owner_active_in(s: AsyncSession, kind: StoredKind, row: Item) -> None:
    if row.owner is not None:
        try:
            await active_row_in(s, row.owner)
        except NotFound:
            raise NotFound(f"{label(kind)} {row.name!r} not found") from None


async def creatable_in(s: AsyncSession, kind: StoredKind, name: str, owner: str | None) -> None:
    """只新建、不覆盖之前（在写入的同一个事务里查）：同名的已存在 Conflict；新条目将归 owner（agent 时检查它还可用）。"""
    if await item_in(s, kind, name) is not None:
        raise Conflict(f"{label(kind)} {name!r} already exists; names are unique, choose another name")
    if owner is not None:
        await active_row_in(s, owner)


async def items_in(s: AsyncSession, kind: StoredKind) -> list[Item]:
    """这一种的全部条目，按名称。"""
    return list(await s.scalars(select(Item).where(Item.kind == kind.value).order_by(Item.name)))


async def visible_in(s: AsyncSession) -> list[Item]:
    """全部条目（正在删除的 agent 的除外），按种类、名称：条目页。"""
    return list(
        await s.scalars(
            select(Item)
            .where(Item.owner.is_(None) | Item.owner.not_in(deleting_ids()))
            .order_by(Item.kind, Item.name)
        )
    )


async def keys_in(s: AsyncSession) -> list[tuple[Kind, str, str | None]]:
    """全部条目的 (种类, 名字, owner)，按种类、名称：算一份配置能用哪些（agents.access.reach_in）。"""
    rows = await s.execute(select(Item.kind, Item.name, Item.owner).order_by(Item.kind, Item.name))
    return [(Kind(k), n, o) for k, n, o in rows]


async def patterns_in(s: AsyncSession) -> list[str]:
    """全部条目的 `kind:name`：新写的分发规则要至少匹配一个（agents.roles.check_new_items_in）。"""
    return [pattern_of(Kind(k), n) for k, n in await s.execute(select(Item.kind, Item.name))]


async def owned_in(s: AsyncSession, owner: str) -> list[Item]:
    """owner 名下的全部条目（删除 agent 时，agents.ownership.retire_owned_in）。"""
    return list(await s.scalars(select(Item).where(Item.owner == owner).order_by(Item.kind, Item.name)))


async def owner_label_in(s: AsyncSession, owner: str | None) -> str:
    """给模型与用户看的 owner：agent 名，或 platform。"""
    if owner is None:
        return "the platform"
    name = await agent_name_in(s, owner)
    return f"agent {name if name is not None else owner!r}"
