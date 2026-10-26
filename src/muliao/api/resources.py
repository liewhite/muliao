"""条目与记忆的版本历史与恢复（表 revisions，见 muliao.agents.revisions）。

记忆（全局与 agent 的）在 /memory 修改，skill 与 MCP server 在各自的接口修改；这里只提供历史与恢复。secret 的版本
不含值，只能看历史，不能恢复。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.agents.api import USER, RevisionInfo, Subject
from muliao.api.deps import C, check_owner
from muliao.errors import InvalidInput

router = APIRouter()

HISTORY_LIMIT = 50


class ResourceKind(StrEnum):
    MEMORY = "memory"
    SKILL = "skill"
    MCP = "mcp"
    SECRET = "secret"


@dataclass(frozen=True)
class _Resource:
    subject: Subject
    # 版本表里的名字
    name: str
    # 把某一版的文档作为新的一版写入；None 为不能恢复
    restore: Callable[[str, str], Awaitable[object]] | None


async def _resource(c: C, kind: ResourceKind, owner: str | None, name: str | None) -> _Resource:
    match kind:
        case ResourceKind.MEMORY:
            # owner 为 None 即全局记忆；读历史与恢复之前确认 agent 还在
            await check_owner(c, owner)
            return _Resource(
                Subject.MEMORY, owner or "", lambda doc, author: c.memory.write(owner, doc, author)
            )
        case ResourceKind.SKILL | ResourceKind.MCP | ResourceKind.SECRET:
            if name is None:
                raise InvalidInput(f"{kind.value} history needs a name")
            # 条目名按种类全局唯一，不需要 owner
            restore: Callable[[str, str], Awaitable[object]] | None = {
                ResourceKind.SKILL: lambda doc, author: c.skills.restore(name, doc, author),
                ResourceKind.MCP: lambda doc, author: c.mcp.restore(name, doc, author),
                ResourceKind.SECRET: None,
            }[kind]
            return _Resource(Subject(kind.value), name, restore)


class RevisionOut(BaseModel):
    rev: int
    # 这一版之后的文档；删除那一版为 null。记忆是正文，skill 是全部文件的 JSON，MCP server 是配置 JSON，secret 只有描述
    content: str | None
    # 这一版之前的文档；新建那一版为 null
    before: str | None
    # "user"，或做出修改的 agent 的 id
    author: str
    message: str
    created_at: datetime

    @classmethod
    def of(cls, r: RevisionInfo) -> "RevisionOut":
        return cls(
            rev=r.id, content=r.after, before=r.before, author=r.author, message=r.message, created_at=r.at
        )


class RestoreIn(BaseModel):
    owner: str | None = None
    name: str | None = None
    rev: int


@router.get("/resources/{kind}/history")
async def history(
    c: C, kind: ResourceKind, owner: str | None = None, name: str | None = None
) -> list[RevisionOut]:
    """记忆按 owner（缺省为全局记忆），条目按 name；新的在前，第一条即当前的一版。只列最近 50 版。"""
    resource = await _resource(c, kind, owner, name)
    return [
        RevisionOut.of(r) for r in await c.revisions.history(resource.subject, resource.name, HISTORY_LIMIT)
    ]


@router.post("/resources/{kind}/restore", status_code=204)
async def restore(c: C, kind: ResourceKind, body: RestoreIn) -> None:
    """把某一版之后的内容作为新的一版写入。"""
    resource = await _resource(c, kind, body.owner, body.name)
    if resource.restore is None:
        raise InvalidInput(f"{kind.value} revisions have no values and cannot be restored")
    revision = await c.revisions.get(resource.subject, resource.name, body.rev)
    if revision.after is None:
        raise InvalidInput(f"revision {body.rev} is a deletion; there is nothing to restore")
    await resource.restore(revision.after, USER)
