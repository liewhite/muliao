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
    unread: bool
    created_at: datetime

    @classmethod
    def of(cls, n: Notification) -> "NotificationOut":
        return cls(
            id=n.id,
            title=n.title,
            body=n.body,
            session_id=n.session_id,
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
