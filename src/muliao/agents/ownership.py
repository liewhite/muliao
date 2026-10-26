"""条目（skill、MCP server、secret）的归属：owner 为 None 是平台（公共）的，否则属于该 agent。名字按种类全局唯一，
没有同名遮蔽。

可见性的过渡规则（#235 改为按角色分发之前，保持原有行为）：agent 看得到自己的与公共的，看不到别的 agent 的。
这条规则只在 sees() / visible_owners() 写一次（前者判断一项，后者是同一条规则的查询条件）。
"""

from typing import Literal

from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import InstrumentedAttribute

Scope = Literal["private", "public"]


def scope_of(owner: str | None) -> Scope:
    return "public" if owner is None else "private"


def sees(agent_id: str | None, owner: str | None) -> bool:
    """agent_id（None 为只看公共）看得到 owner 的条目。"""
    return owner is None or owner == agent_id


def owned_by(column: InstrumentedAttribute[str | None], owner: str | None) -> ColumnElement[bool]:
    """查询条件：归属恰为 owner（None 即公共）。"""
    return column.is_(None) if owner is None else column == owner


def visible_owners(column: InstrumentedAttribute[str | None], agent_id: str | None) -> ColumnElement[bool]:
    """查询条件：agent_id 看得到的归属（同 sees）。"""
    return column.is_(None) if agent_id is None else or_(column.is_(None), column == agent_id)
