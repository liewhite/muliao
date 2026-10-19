"""prompt 的版本历史：公共 prompt 的修改，以及任意版本（公共或 agent 指令）的恢复。

agent 指令随 agent 配置一起修改（PUT /agents/{id}），这里只提供它的历史与恢复，不另开写入口。
"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.evolution.prompts import USER
from muliao.models import PromptRevision

router = APIRouter()


class RevisionOut(BaseModel):
    id: int
    content: str
    # "user"，或做出修改的 agent 的 id
    author: str
    created_at: datetime

    @classmethod
    def of(cls, r: PromptRevision) -> "RevisionOut":
        return cls(id=r.id, content=r.content, author=r.author, created_at=r.created_at)


class PublicPromptIn(BaseModel):
    content: str


@router.get("/prompts")
async def prompt_history(c: C, owner: str | None = None) -> list[RevisionOut]:
    """owner 为 agent id 时是它的指令历史，缺省时是公共 prompt 的历史；新的在前。"""
    if owner is not None:
        await c.agents.get(owner)
    return [RevisionOut.of(r) for r in await c.prompts.history(owner)]


@router.put("/prompts/public", status_code=204)
async def set_public_prompt(c: C, body: PublicPromptIn) -> None:
    await c.prompts.set(None, body.content, USER)


@router.post("/prompt-revisions/{revision_id}/restore", status_code=204)
async def restore_revision(c: C, revision_id: int) -> None:
    await c.prompts.restore(revision_id, USER)
