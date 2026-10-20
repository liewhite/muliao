from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.api.files import FileOut
from muliao.container import Container
from muliao.errors import NotFound
from muliao.models import Mail, MailKind, MailState, Origin, Session, Task, TaskKind, TaskStatus
from muliao.runtime.conversation import Inbound, NewSession, answerable_questions
from muliao.runtime.mailbox import latest_run, visible_in
from muliao.runtime.pending import CallResult, call_result
from muliao.runtime.questions import Question, QuestionAnswer
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
    not_before: datetime
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, t: Task) -> "TaskOut":
        return cls(
            id=t.id,
            kind=t.kind,
            agent_id=t.agent_id,
            session_id=t.session_id,
            status=t.status,
            attempts=t.attempts,
            error=t.error,
            not_before=t.not_before,
            created_at=t.created_at,
            finished_at=t.finished_at,
        )


class MailOut(BaseModel):
    """会话信箱里的一封信件（runtime.mailbox）。"""

    id: int
    kind: MailKind
    # 事件的发送方（如 job:12）；用户消息与回答为 null
    source: str | None
    content: str
    attachments: list[FileOut]
    # 回答信件：tool_call_id -> 那次挂起调用的结果（用户的回答，或被问 agent 的结局）
    answers: dict[str, CallResult]
    state: MailState
    created_at: datetime


async def mail_outs(c: Container, mail: list[Mail]) -> list[MailOut]:
    """MailOut 的唯一构造入口：一次取齐这些信件的附件。"""
    ids = list(dict.fromkeys(i for m in mail for i in m.attachments))
    files = {f.id: FileOut.of(f) for f in await c.files.referenced(ids)}
    return [
        MailOut(
            id=m.id,
            kind=m.kind,
            source=m.source,
            content=m.content,
            attachments=[files[i] for i in m.attachments],
            answers={k: call_result(v) for k, v in m.answers.items()},
            state=m.state,
            created_at=m.created_at,
        )
        for m in mail
    ]


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
    # 显示在历史之后的信件：排队中的（处理中的用户消息、已提交的回答），以及最近一次停止丢弃的
    mail: list[MailOut]
    # 最近一次运行，还没正常结束时（排队、运行中、等重试或失败）；前端据此显示「处理中」与错误、重试
    run: TaskOut | None
    # 这个会话开出的 thread
    threads: list[SessionOut]
    # 现在可以回答的提问（已提交、还没交给模型的回答显示在 mail 里）
    pending_questions: list[PendingQuestionsOut]
    # 正在进行的运行当前这一段的事件（与 SSE 的 run 事件同一形状，见 bus.LiveRuns），让中途打开页面也能从这一段
    # 的开头看起；没有则为 null
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
            awaited=True,
            attachments=tuple(body.attachments),
        )
    )
    return SessionOut.of(received.session)


@router.get("/sessions/{session_id}")
async def get_session(c: C, session_id: str) -> SessionDetail:
    session = await c.sessions.get(session_id)
    # 先取快照再取历史：快照这一段已在展示的回复不在历史里重复显示（bus.LiveRuns）；之间换了段时，新保存的消息
    # 照旧由旧快照展示，saved 事件随后让前端重新拉取
    run = c.live.snapshot(session_id)
    history = await c.sessions.history_with_ids(session_id)
    items = [
        {**item, "message_id": message_id}
        for message_id, message in history
        if run is None or run.after_message_id is None or message_id <= run.after_message_id
        for item in to_items([message])
    ]
    compactions = [
        CompactionOut(through_message_id=x.through_message_id, summary=x.summary, created_at=x.created_at)
        for x in await c.sessions.compactions(session_id)
    ]
    async with c.db.transaction() as s:
        mail = await visible_in(s, session_id)
        latest = await latest_run(s, session_id)
        # 与 receive 同一个判断：已经提交了回答的提问不再显示表单
        pending = await answerable_questions(s, session_id)
    return SessionDetail(
        session=SessionOut.of(session),
        items=items,
        compactions=compactions,
        mail=await mail_outs(c, mail),
        run=None if latest is None or latest.status == TaskStatus.DONE else TaskOut.of(latest),
        threads=[SessionOut.of(t) for t in await c.sessions.threads(session_id)],
        live=None if run is None else LiveRunOut(run_id=run.run_id, events=run.events),
        pending_questions=[
            PendingQuestionsOut(tool_call_id=call_id, questions=questions)
            for call_id, questions in pending.items()
        ],
    )


@router.post("/sessions/{session_id}/messages", status_code=202)
async def reply(c: C, session_id: str, body: ReplyIn) -> list[MailOut]:
    received = await c.sessions.receive(
        Inbound(
            origin=Origin.USER,
            origin_ref=None,
            content=body.prompt,
            session=session_id,
            awaited=True,
            attachments=tuple(body.attachments),
            answers=body.answers,
        )
    )
    return await mail_outs(c, received.mail)


@router.post("/sessions/{session_id}/stop", status_code=202)
async def stop(c: C, session_id: str) -> None:
    """停止这个会话：结束正在进行的运行（已经产生的部分照常保存）、清空信箱，连带停下它发起的异步工作
    （docs/architecture.md「停止会话」）。"""
    await c.sessions.get(session_id)
    await c.consumer.stop_session(session_id)


class CompactOut(BaseModel):
    # False 表示已有待执行的压缩，本次未重复投递
    queued: bool


@router.post("/sessions/{session_id}/compact", status_code=202)
async def compact(c: C, session_id: str) -> CompactOut:
    """手动压缩上下文：上下文超限导致运行失败时，压缩后再重试。"""
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
    return [TaskOut.of(t) for t in await c.queue.list(status=status, session_id=None, limit=limit)]


@router.post("/tasks/{task_id}/retry")
async def retry_task(c: C, task_id: int) -> TaskOut:
    return TaskOut.of(await c.queue.retry(task_id))
