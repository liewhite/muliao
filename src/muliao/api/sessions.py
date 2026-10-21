from datetime import datetime

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.api.files import FileOut
from muliao.container import Container
from muliao.platform.tables import Task, TaskStatus
from muliao.platform.tooling import Origin
from muliao.sessions.api import (
    CallResult,
    Inbound,
    Item,
    LiveRun,
    NewSession,
    QuestionAnswer,
    RunEvent,
    SessionKind,
    WorkState,
    call_result,
    default_title,
    policy_of,
)
from muliao.sessions.tables import Compaction, Mail, MailKind, MailState, Session

router = APIRouter()


class SessionOut(BaseModel):
    id: str
    agent_id: str
    title: str
    origin: Origin
    origin_ref: str | None
    # 委派产生的子会话：父会话与 thread 名；种类决定规则（private_thread：ask_agent 开出的、用户只读的 thread）
    parent_session_id: str | None
    thread: str | None
    kind: SessionKind
    # 种类的规则（sessions.kinds.KindPolicy）随会话下发，界面不另写一份：用户能否在里面发消息、能否单独删除、是否私有
    user_writable: bool
    deletable: bool
    # 私有 thread：只有开它的会话能继续
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
            kind=s.kind,
            user_writable=policy_of(s.kind).user_writable,
            deletable=not policy_of(s.kind).owned_by_parent,
            private=policy_of(s.kind).private,
            unread=s.unread,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )


class SessionListItem(SessionOut):
    """会话列表里的一行：多一个只在列表里算的工作状态（在运行 / 在等后台任务、委派或用户回答 / 空闲；会话页看
    SessionDetail.run）。"""

    state: WorkState


class TaskOut(BaseModel):
    id: int
    kind: str
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
    """会话信箱里的一封信件（sessions.mailbox）。"""

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

    @classmethod
    def of(cls, x: Compaction) -> "CompactionOut":
        return cls(through_message_id=x.through_message_id, summary=x.summary, created_at=x.created_at)


class LiveRunOut(BaseModel):
    run_id: str
    events: list[RunEvent]

    @classmethod
    def of(cls, run: LiveRun) -> "LiveRunOut":
        return cls(run_id=run.run_id, events=run.events)


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
    # 正在进行的运行当前这一段的事件（与 SSE 的 run 事件同一形状，见 sessions.live.LiveRuns），让中途打开页面也能从这一段
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
    # 对挂起提问的回答（条目里 ask.status 为 pending 的提问）；只回答时 prompt 可以为空
    answers: dict[str, list[QuestionAnswer]] = {}


@router.get("/sessions")
async def list_sessions(
    c: C,
    agent_id: str | None = None,
    origin: list[Origin] | None = Query(None),
    origin_ref: str | None = Query(None, description="发起方在它那个 origin 里的身份，如触发器的 id"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[SessionListItem]:
    """正在运行的会话置顶，其余按最近更新排序。"""
    sessions = await c.sessions.list(
        agent_id=agent_id, origins=origin, origin_ref=origin_ref, limit=limit, offset=offset
    )
    states = await c.sessions.work_states([s.id for s in sessions])
    return [SessionListItem(**SessionOut.of(s).model_dump(), state=states[s.id]) for s in sessions]


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
    view = await c.sessions.detail(session_id)
    return SessionDetail(
        session=SessionOut.of(view.session),
        items=view.items,
        compactions=[CompactionOut.of(x) for x in view.compactions],
        mail=await mail_outs(c, view.mail),
        run=None if view.run is None else TaskOut.of(view.run),
        threads=[SessionOut.of(t) for t in view.threads],
        live=None if view.live is None else LiveRunOut.of(view.live),
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


@router.post("/sessions/{session_id}/stop", status_code=202, response_class=Response)
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
    """手动压缩上下文（自动压缩见 sessions.compaction）：排在正在进行与排队的运行之后。"""
    return CompactOut(queued=await c.compactor.request(session_id))


@router.post("/sessions/{session_id}/read", status_code=204)
async def mark_read(c: C, session_id: str) -> None:
    """标记会话已读；指向它的通知由通知领域随 SessionRead 标为已读。"""
    await c.sessions.mark_read(session_id)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(c: C, session_id: str) -> None:
    await c.sessions.delete(session_id)


@router.get("/tasks")
async def list_tasks(
    c: C, status: TaskStatus | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[TaskOut]:
    return [TaskOut.of(t) for t in await c.queue.list(status=status, session_id=None, limit=limit)]


@router.post("/tasks/{task_id}/retry")
async def retry_task(c: C, task_id: int) -> TaskOut:
    return TaskOut.of(await c.queue.retry(task_id))
