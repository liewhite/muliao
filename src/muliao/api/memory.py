"""记忆（agents·记忆子域）的接口：全局记忆与各 agent 的记忆，读写、版本历史与恢复。写入的人记为 "user"。

版本的输出格式与条目共用（api/revisions.py）。
"""

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.agents.api import USER
from muliao.api.deps import C
from muliao.api.revisions import HISTORY_LIMIT, RevisionOut

router = APIRouter()


class MemoryOut(BaseModel):
    content: str
    max_chars: int


class MemoryIn(BaseModel):
    # 为空即全局记忆
    agent: str | None
    content: str


class MemoryRestoreIn(BaseModel):
    # 为空即全局记忆
    agent: str | None = None
    rev: int


@router.get("/memory")
async def get_memory(c: C, agent: str | None = None) -> MemoryOut:
    """agent 为空是全局记忆（所有 agent 共享），否则是该 agent 的记忆。"""
    return MemoryOut(content=await c.memory.read(agent), max_chars=c.memory.max_chars)


@router.put("/memory")
async def put_memory(c: C, body: MemoryIn) -> MemoryOut:
    return MemoryOut(
        content=await c.memory.write(body.agent, body.content, USER), max_chars=c.memory.max_chars
    )


@router.get("/memory/history")
async def memory_history(c: C, agent: str | None = None) -> list[RevisionOut]:
    """agent 为空是全局记忆；新的在前，第一条即当前的一版，只列最近 50 版。"""
    return [RevisionOut.of(r) for r in await c.memory.history(agent, HISTORY_LIMIT)]


@router.post("/memory/restore", status_code=204)
async def restore_memory(c: C, body: MemoryRestoreIn) -> None:
    """把某一版的正文作为新的一版写入。"""
    await c.memory.restore(body.agent, body.rev, USER)
