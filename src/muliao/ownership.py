"""agent 资源（skill、MCP server、secret）的归属与合并规则：owner 为 None 即公共，否则为该 agent 私有。

agent 看到的是自己的私有资源加上公共资源；同名时私有遮蔽公共。这条规则只在 visible() 实现一次。
"""

from collections.abc import Callable, Iterable
from typing import Literal

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
    """
    items = [i for i in items if owner(i) is None or (agent_id is not None and owner(i) == agent_id)]
    private = {name(i) for i in items if owner(i) is not None}
    return [i for i in items if owner(i) is not None or name(i) not in private]
