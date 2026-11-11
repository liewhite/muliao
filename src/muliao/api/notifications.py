from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.notifications.tables import Notification

router = APIRouter(prefix="/notifications")


class NotificationOut(BaseModel):
    id: int
    title: str
    body: str
    session_id: str | None
    # 交给用户审核的 issue
    issue_id: int | None
    # 点开打开的页面（与推送的一致）；系统通知为 null
    url: str | None
    unread: bool
    created_at: datetime

    @classmethod
    def of(cls, n: Notification) -> "NotificationOut":
        return cls(
            id=n.id,
            title=n.title,
            body=n.body,
            session_id=n.session_id,
            issue_id=n.issue_id,
            url=n.url,
            unread=n.unread,
            created_at=n.created_at,
        )


class UnreadOut(BaseModel):
    count: int


class MarkReadIn(BaseModel):
    # 用户看到的最新一条；之后才到的保持未读
    through_id: int


@router.get("")
async def list_notifications(c: C, limit: int = Query(100, ge=1, le=500)) -> list[NotificationOut]:
    return [NotificationOut.of(n) for n in await c.notifications.list(limit)]


@router.get("/unread")
async def unread(c: C) -> UnreadOut:
    return UnreadOut(count=await c.notifications.unread_count())


@router.post("/read", status_code=204)
async def mark_read(c: C, body: MarkReadIn) -> None:
    await c.notifications.mark_read(body.through_id)


@router.post("/issues/{issue_id}/read", status_code=204)
async def mark_issue_read(c: C, issue_id: int) -> None:
    """打开了 issue：请用户审核它的通知标为已读。"""
    await c.notifications.mark_issue_read(issue_id)
