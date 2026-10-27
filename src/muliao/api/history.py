"""工作历史接口：界面在 agent 页列出它的历史（只读；写入只经 agent 的 history_append）。"""

from datetime import date, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.history.api import HistoryRecord

router = APIRouter()


class HistoryOut(BaseModel):
    id: int
    agent_id: str
    day: date
    title: str
    summary: str
    refs: list[str]
    importance: int
    created_at: datetime

    @classmethod
    def of(cls, r: HistoryRecord) -> "HistoryOut":
        return cls(
            id=r.id,
            agent_id=r.agent_id,
            day=r.day,
            title=r.title,
            summary=r.summary,
            refs=list(r.refs),
            importance=r.importance,
            created_at=r.created_at,
        )


@router.get("/agents/{agent_id}/history")
async def agent_history(
    c: C,
    agent_id: str,
    query: str | None = None,
    since: date | None = None,
    until: date | None = None,
    min_importance: int = 1,
    limit: int = 100,
) -> list[HistoryOut]:
    """agent 的工作历史：没给 query 按日期倒序；给了按关键词检索（规则同工具 history_search）。since / until 含两端。"""
    # 正在删除的 agent 按不存在处理
    await c.agents.get_active(agent_id)
    found = await c.history.search(
        agent_ids=[agent_id],
        query=query,
        since=since,
        until=until,
        min_importance=min_importance,
        limit=limit,
    )
    return [HistoryOut.of(r) for r in found]
