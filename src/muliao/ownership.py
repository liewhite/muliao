"""skill 与 MCP server 的归属规则：owner 为 None 即公共，否则为该 agent 私有。"""

from typing import Literal

Scope = Literal["private", "public"]


def scope_of(owner: str | None) -> Scope:
    return "public" if owner is None else "private"


def owner_of(scope: Scope, agent_id: str) -> str | None:
    return agent_id if scope == "private" else None
