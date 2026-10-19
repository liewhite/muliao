"""agent 资源（skill、MCP server、secret）的归属与合并规则：owner 为 None 即公共，否则为该 agent 私有。

agent 看到的是自己的私有资源加上公共资源；同名时私有遮蔽公共。这条规则只在 visible() 实现一次。
"""

from collections.abc import Callable, Iterable
from typing import Literal

from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import InstrumentedAttribute

Scope = Literal["private", "public"]


def scope_of(owner: str | None) -> Scope:
    return "public" if owner is None else "private"


def owner_of(scope: Scope, agent_id: str) -> str | None:
    return agent_id if scope == "private" else None


def visible[T](
    items: Iterable[T], *, agent_id: str | None, owner: Callable[[T], str | None], name: Callable[[T], str]
) -> list[T]:
    """从若干资源中选出 agent 可见的：自己私有的，以及未被同名私有资源遮蔽的公共资源；保持输入顺序。

    agent_id 为 None 时只看公共资源（例如解析公共 MCP server 配置里的引用）。其他 agent 的私有资源一律不可见。
    规则以这里为准；查询时用 visible_owners 预先缩小范围只是性能优化。
    """
    items = [i for i in items if owner(i) is None or (agent_id is not None and owner(i) == agent_id)]
    private = {name(i) for i in items if owner(i) is not None}
    return [i for i in items if owner(i) is not None or name(i) not in private]


def owned_by(column: InstrumentedAttribute[str | None], owner: str | None) -> ColumnElement[bool]:
    """查询条件：归属恰为 owner（None 即公共）。"""
    return column.is_(None) if owner is None else column == owner


def visible_owners(column: InstrumentedAttribute[str | None], agent_id: str | None) -> ColumnElement[bool]:
    """查询条件：可能对 agent_id 可见的归属（公共，或它自己）。最终可见性仍由 visible() 决定。"""
    return column.is_(None) if agent_id is None else or_(column.is_(None), column == agent_id)
