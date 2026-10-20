from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.api.files import FileOut
from muliao.container import Container
from muliao.errors import Conflict, NotFound
from muliao.models import Origin, ReplyTo, Session, Task, TaskKind, TaskStatus
from muliao.runtime.conversation import Inbound, NewSession, TurnPayload, answerable_questions
from muliao.runtime.questions import AskResult, Question, QuestionAnswer
from muliao.runtime.sessions import default_title
from muliao.transcript import Item, to_items

router = APIRouter()


class SessionOut(BaseModel):
    id: str
    agent_id: str
    title: str
    origin: Origin
    origin_ref: str | None
    # 委派产生的子会话：父会话与 thread 名；private 为 ask_agent 开出的、用户只读的 thread
    parent_session_id: str | None
    thread: str | None
    private: bool
    unread: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, s: Session) -> "SessionOut":
        return cls(
            id=s.id,
            agent_id=s.agent_id,
            title=s.title,
            origin=s.origin,
            origin_ref=s.origin_ref,
            parent_session_id=s.parent_session_id,
            thread=s.thread,
            private=s.private,
            unread=s.unread,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )


class TaskOut(BaseModel):
    id: int
    kind: TaskKind
    agent_id: str | None
    session_id: str | None
    status: TaskStatus
    attempts: int
    error: str | None
    prompt: str | None
    # turn 随消息发送的文件
    attachments: list[FileOut]
    # turn 带的对挂起提问的回答：tool_call_id -> 交给模型的结果
    answers: dict[str, AskResult]
    not_before: datetime
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, t: Task, files: dict[str, FileOut]) -> "TaskOut":
        turn = _turn_payload(t)
        return cls(
            id=t.id,
            kind=t.kind,
            agent_id=t.agent_id,
            session_id=t.session_id,
            status=t.status,
            attempts=t.attempts,
            error=t.error,
            prompt=None if turn is None else turn.prompt,
            attachments=[] if turn is None else [files[i] for i in turn.attachments],
            answers={} if turn is None else turn.answers,
            not_before=t.not_before,
            created_at=t.created_at,
            finished_at=t.finished_at,
        )


def _turn_payload(t: Task) -> TurnPayload | None:
    """只有 turn 有输入（prompt、附件、回答）；其他种类的 task 没有。"""
    return TurnPayload.of(t) if t.kind == TaskKind.TURN else None


async def task_outs(c: Container, tasks: list[Task]) -> list[TaskOut]:
    """TaskOut 的唯一构造入口：一次取齐这些 task 的附件。"""
    ids = list(dict.fromkeys(i for t in tasks if (turn := _turn_payload(t)) for i in turn.attachments))
    files = {f.id: FileOut.of(f) for f in await c.files.referenced(ids)}
    return [TaskOut.of(t, files) for t in tasks]


class CompactionOut(BaseModel):
    through_message_id: int
    summary: str
    created_at: datetime


class PendingQuestionsOut(BaseModel):
    """一次还在等用户回答的 ask_user 提问；回答时用 tool_call_id 引用它。"""

    tool_call_id: str
    questions: list[Question]


class LiveRunOut(BaseModel):
    run_id: str
    events: list[dict[str, Any]]


class SessionDetail(BaseModel):
    session: SessionOut
    items: list[Item]
    # 压缩点：through_message_id 及之前的消息已由 summary 代替发给模型（原文仍完整展示）
    compactions: list[CompactionOut]
    # 尚未完成或失败的轮次：前端据此显示「处理中」的用户消息与错误
    open_turns: list[TaskOut]
    # 这个会话开出的 thread
    threads: list[SessionOut]
    # 现在可以回答的提问（已提交的回答显示在 open_turns 里）
    pending_questions: list[PendingQuestionsOut]
    # 正在进行的这一轮已经产生的运行事件（与 SSE 的 run 事件同一形状），让中途打开页面也能从头看起；没有则为 null
    live: LiveRunOut | None


class StartIn(BaseModel):
    agent_id: str
    prompt: str
    # 先经 POST /api/files 上传得到的 id
    attachments: list[str] = []


class ReplyIn(BaseModel):
    prompt: str
    attachments: list[str] = []
    # 对挂起提问的回答（见 SessionDetail.pending_questions）；只回答时 prompt 可以为空
    answers: dict[str, list[QuestionAnswer]] = {}


@router.get("/sessions")
async def list_sessions(
    c: C,
    agent_id: str | None = None,
    origin: list[Origin] | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[SessionOut]:
    sessions = await c.sessions.list(agent_id=agent_id, origins=origin, limit=limit, offset=offset)
    return [SessionOut.of(s) for s in sessions]


@router.post("/sessions", status_code=201)
async def start_session(c: C, body: StartIn) -> SessionOut:
    # 只发了文件时，用文件名作标题
    names = ", ".join(f.name for f in await c.files.get_many(body.attachments))
    received = await c.sessions.receive(
        Inbound(
            origin=Origin.USER,
            origin_ref=None,
            content=body.prompt,
            session=NewSession(agent_id=body.agent_id, title=default_title(body.prompt.strip() or names)),
            reply_to=ReplyTo.USER,
            attachments=tuple(body.attachments),
        )
    )
    return SessionOut.of(received.session)


@router.get("/sessions/{session_id}")
async def get_session(c: C, session_id: str) -> SessionDetail:
    session = await c.sessions.get(session_id)
    history = await c.sessions.history_with_ids(session_id)
    items = [
        {**item, "message_id": message_id} for message_id, message in history for item in to_items([message])
    ]
    compactions = [
        CompactionOut(through_message_id=x.through_message_id, summary=x.summary, created_at=x.created_at)
        for x in await c.sessions.compactions(session_id)
    ]
    tasks = await c.queue.list(status=None, session_id=session_id, limit=50)
    open_tasks = [t for t in reversed(tasks) if t.kind == TaskKind.TURN and t.status != TaskStatus.DONE]
    open_turns = await task_outs(c, open_tasks)
    # 与 receive 同一个判断：有会先执行的轮次时提问已不能回答，不显示表单
    async with c.db.transaction() as s:
        pending = await answerable_questions(s, session_id) or {}
    return SessionDetail(
        session=SessionOut.of(session),
        items=items,
        compactions=compactions,
        open_turns=open_turns,
        threads=[SessionOut.of(t) for t in await c.sessions.threads(session_id)],
        live=None
        if (run := c.live.snapshot(session_id)) is None
        else LiveRunOut(run_id=run.run_id, events=run.events),
        pending_questions=[
            PendingQuestionsOut(tool_call_id=call_id, questions=questions)
            for call_id, questions in pending.items()
        ],
    )


@router.post("/sessions/{session_id}/messages", status_code=202)
async def reply(c: C, session_id: str, body: ReplyIn) -> TaskOut:
    received = await c.sessions.receive(
        Inbound(
            origin=Origin.USER,
            origin_ref=None,
            content=body.prompt,
            session=session_id,
            reply_to=ReplyTo.USER,
            attachments=tuple(body.attachments),
            answers=body.answers,
        )
    )
    return (await task_outs(c, [received.turn]))[0]


@router.post("/sessions/{session_id}/stop", status_code=202)
async def stop(c: C, session_id: str) -> None:
    """停止这个会话正在运行的一轮。停止前已经产生的部分（文字、工具调用与结果）照常保存。
    私有 thread 的轮次属于发问的 agent（在它那一轮里内联运行），只能停止发问方所在的会话。"""
    session = await c.sessions.get(session_id)
    if session.private:
        raise Conflict(f"session {session_id!r} is a private thread; stop the asking session instead")
    if not c.turns.stop(session_id):
        raise Conflict(f"session {session_id!r} has no running turn")


class CompactOut(BaseModel):
    # False 表示已有待执行的压缩，本次未重复投递
    queued: bool


@router.post("/sessions/{session_id}/compact", status_code=202)
async def compact(c: C, session_id: str) -> CompactOut:
    """手动压缩上下文：单轮内上下文超限导致失败时，压缩后再重试该轮。"""
    async with c.db.transaction() as s:
        session = await s.get(Session, session_id)
        if session is None:
            raise NotFound(f"session {session_id!r} not found")
        return CompactOut(queued=await c.compaction.request(s, session.id, session.agent_id))


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
    return await task_outs(c, await c.queue.list(status=status, session_id=None, limit=limit))


@router.post("/tasks/{task_id}/retry")
async def retry_task(c: C, task_id: int) -> TaskOut:
    return (await task_outs(c, [await c.queue.retry(task_id)]))[0]
