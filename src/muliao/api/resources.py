"""agent 能改的文本资源的版本历史与恢复（git，见 muliao.agents.resources）。

记忆（全局与 agent 的）在 /memory 修改；这里只提供历史与恢复。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.agents.api import USER, CorruptResource, Revision
from muliao.api.deps import C, check_owner

router = APIRouter()


class CorruptOut(BaseModel):
    """一个读不出来的资源文件（agents.api.CorruptResource）：界面列出来，供删除（skill 也可重写）。"""

    owner: str | None
    name: str
    problem: str

    @classmethod
    def of(cls, c: CorruptResource) -> "CorruptOut":
        return cls(owner=c.owner, name=c.name, problem=c.problem)


HISTORY_LIMIT = 50


class ResourceKind(StrEnum):
    MEMORY = "memory"


@dataclass(frozen=True)
class _Resource:
    path: Path
    # 写入一版（含校验，如记忆的长度上限）
    write: Callable[[str, str], Awaitable[object]]


async def _resource(c: C, kind: ResourceKind, owner: str | None) -> _Resource:
    # 读历史与恢复一版之前；恢复的写入仍由资源仓库在锁里检查（agents.active）
    await check_owner(c, owner)
    match kind:
        case ResourceKind.MEMORY:
            # owner 为 None 即全局记忆
            return _Resource(
                c.memory.path(owner), lambda content, author: c.memory.write(owner, content, author)
            )


class RevisionOut(BaseModel):
    rev: str
    content: str
    # "user"，或做出修改的 agent 的 id
    author: str
    created_at: datetime

    @classmethod
    def of(cls, r: Revision, content: str) -> "RevisionOut":
        return cls(rev=r.rev, content=content, author=r.author, created_at=r.at)


class RestoreIn(BaseModel):
    owner: str | None = None
    rev: str


@router.get("/resources/{kind}/history")
async def history(c: C, kind: ResourceKind, owner: str | None = None) -> list[RevisionOut]:
    """owner 缺省为公共资源；新的在前，第一条即当前生效的一版。只列最近 50 版，更早的仍保留在仓库里。"""
    resource = await _resource(c, kind, owner)
    revisions = await c.resources.history(resource.path, HISTORY_LIMIT)
    contents = await c.resources.contents_at([r.rev for r in revisions], resource.path)
    return [RevisionOut.of(r, content) for r, content in zip(revisions, contents, strict=True)]


@router.post("/resources/{kind}/restore", status_code=204)
async def restore(c: C, kind: ResourceKind, body: RestoreIn) -> None:
    """把某一版的内容作为新的一版写入。"""
    resource = await _resource(c, kind, body.owner)
    await resource.write(await c.resources.read_at(body.rev, resource.path), USER)
