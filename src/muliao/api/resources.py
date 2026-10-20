"""agent 能改的文本资源的版本历史与恢复（git，见 muliao.agents.resources），以及公共 prompt 的编辑。

agent 的指令随 agent 配置修改（PUT /agents/{id}），记忆在 /agents/{id}/memory 修改；这里只提供历史与恢复。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.agents.resources import USER
from muliao.api.deps import C
from muliao.errors import InvalidInput

router = APIRouter()

HISTORY_LIMIT = 50


class ResourceKind(StrEnum):
    PROMPT = "prompt"
    MEMORY = "memory"


@dataclass(frozen=True)
class _Resource:
    path: Path
    # 写入一版（含校验，如记忆的长度上限）
    write: Callable[[str, str], Awaitable[object]]


async def _resource(c: C, kind: ResourceKind, owner: str | None) -> _Resource:
    if owner is not None:
        await c.agents.get(owner)
    match kind:
        case ResourceKind.PROMPT:
            return _Resource(
                c.prompts.path(owner), lambda content, author: c.prompts.set(owner, content, author)
            )
        case ResourceKind.MEMORY:
            if owner is None:
                raise InvalidInput("memory belongs to an agent; owner is required")
            return _Resource(
                c.memory.path(owner), lambda content, author: c.memory.write(owner, content, author)
            )


class RevisionOut(BaseModel):
    rev: str
    content: str
    # "user"，或做出修改的 agent 的 id
    author: str
    created_at: datetime


class RestoreIn(BaseModel):
    owner: str | None = None
    rev: str


class ContentIn(BaseModel):
    content: str


@router.get("/resources/{kind}/history")
async def history(c: C, kind: ResourceKind, owner: str | None = None) -> list[RevisionOut]:
    """owner 缺省为公共资源；新的在前，第一条即当前生效的一版。只列最近 50 版，更早的仍保留在仓库里。"""
    resource = await _resource(c, kind, owner)
    return [
        RevisionOut(
            rev=r.rev,
            content=await c.resources.read_at(r.rev, resource.path),
            author=r.author,
            created_at=r.at,
        )
        for r in await c.resources.history(resource.path, HISTORY_LIMIT)
    ]


@router.post("/resources/{kind}/restore", status_code=204)
async def restore(c: C, kind: ResourceKind, body: RestoreIn) -> None:
    """把某一版的内容作为新的一版写入。"""
    resource = await _resource(c, kind, body.owner)
    await resource.write(await c.resources.read_at(body.rev, resource.path), USER)


@router.put("/prompts/public", status_code=204)
async def set_public_prompt(c: C, body: ContentIn) -> None:
    await c.prompts.set(None, body.content, USER)
