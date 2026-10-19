from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.models import DeliveryChannel, Origin, Session, Task, TaskKind, TaskStatus
from muliao.transcript import Item, to_items

router = APIRouter()


class SessionOut(BaseModel):
    id: str
    agent_name: str
    title: str
    origin: Origin
    origin_ref: str | None
    delivery: DeliveryChannel
    unread: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, s: Session) -> "SessionOut":
        return cls(
            id=s.id,
            agent_name=s.agent_name,
            title=s.title,
            origin=s.origin,
            origin_ref=s.origin_ref,
            delivery=s.delivery,
            unread=s.unread,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )


class TaskOut(BaseModel):
    id: int
    kind: TaskKind
    agent_name: str | None
    session_id: str | None
    status: TaskStatus
    attempts: int
    error: str | None
    prompt: str | None
    not_before: datetime
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, t: Task) -> "TaskOut":
        return cls(
            id=t.id,
            kind=t.kind,
            agent_name=t.agent_name,
            session_id=t.session_id,
            status=t.status,
            attempts=t.attempts,
            error=t.error,
            prompt=t.payload.get("prompt") if t.kind == TaskKind.TURN else None,
            not_before=t.not_before,
            created_at=t.created_at,
            finished_at=t.finished_at,
        )


class CompactionOut(BaseModel):
    through_message_id: int
    summary: str
    created_at: datetime


class SessionDetail(BaseModel):
    session: SessionOut
    items: list[Item]
    # 压缩点：through_message_id 及之前的消息已由 summary 代替发给模型（原文仍完整展示）
    compactions: list[CompactionOut]
    # 尚未完成或失败的 turn：前端据此显示「处理中」的用户消息与错误
    open_turns: list[TaskOut]


class StartIn(BaseModel):
    agent_name: str
    prompt: str


class ReplyIn(BaseModel):
    prompt: str


@router.get("/sessions")
async def list_sessions(
    c: C,
    agent: str | None = None,
    origin: list[Origin] | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> list[SessionOut]:
    return [
        SessionOut.of(s)
        for s in await c.sessions.list(agent_name=agent, origins=origin, limit=limit, offset=offset)
    ]


@router.post("/sessions", status_code=201)
async def start_session(c: C, body: StartIn) -> SessionOut:
    session = await c.sessions.start(
        agent_name=body.agent_name,
        prompt=body.prompt,
        origin=Origin.USER,
        origin_ref=None,
        delivery=DeliveryChannel.WEBPUSH,
    )
    return SessionOut.of(session)


@router.get("/sessions/{session_id}")
async def get_session(c: C, session_id: str) -> SessionDetail:
    session = await c.sessions.get(session_id)
    items = [
        {**item, "message_id": message_id}
        for message_id, message in await c.sessions.history_with_ids(session_id)
        for item in to_items([message])
    ]
    compactions = [
        CompactionOut(through_message_id=x.through_message_id, summary=x.summary, created_at=x.created_at)
        for x in await c.sessions.compactions(session_id)
    ]
    tasks = await c.queue.list(status=None, session_id=session_id, limit=50)
    open_turns = [
        TaskOut.of(t)
        for t in reversed(tasks)
        if t.kind == TaskKind.TURN and t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.FAILED)
    ]
    return SessionDetail(
        session=SessionOut.of(session), items=items, compactions=compactions, open_turns=open_turns
    )


@router.post("/sessions/{session_id}/messages", status_code=202)
async def reply(c: C, session_id: str, body: ReplyIn) -> TaskOut:
    return TaskOut.of(await c.sessions.reply(session_id, body.prompt))


@router.post("/sessions/{session_id}/read", status_code=204)
async def mark_read(c: C, session_id: str) -> None:
    await c.sessions.mark_read(session_id)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(c: C, session_id: str) -> None:
    await c.sessions.delete(session_id)


@router.get("/tasks")
async def list_tasks(
    c: C, status: TaskStatus | None = None, limit: int = Query(100, le=500)
) -> list[TaskOut]:
    return [TaskOut.of(t) for t in await c.queue.list(status=status, session_id=None, limit=limit)]


@router.post("/tasks/{task_id}/retry")
async def retry_task(c: C, task_id: int) -> TaskOut:
    return TaskOut.of(await c.queue.retry(task_id))
