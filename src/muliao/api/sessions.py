from datetime import datetime

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.api.files import FileOut
from muliao.api.tasks import TaskOut
from muliao.container import Container
from muliao.sessions.api import (
    CallResult,
    EventSource,
    Item,
    LiveSegment,
    NewChat,
    Origin,
    QuestionAnswer,
    RunEvent,
    SessionKind,
    UserMessage,
    WorkState,
    call_result,
    parse_source,
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
    # 有指向它的未读通知（用户在等的回答、提问、agent 主动通知、运行失败）：未读不另存，从通知派生
    unread: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, s: Session, unread: bool) -> "SessionOut":
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
            deletable=policy_of(s.kind).deletable,
            private=policy_of(s.kind).private,
            unread=unread,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )


class SessionListItem(SessionOut):
    """会话列表里的一行：多一个只在列表里算的工作状态（在运行 / 在等后台任务、委派或用户回答 / 空闲；会话页看
    SessionDetail.run）。"""

    state: WorkState


async def session_outs(c: Container, sessions: list[Session]) -> list[SessionOut]:
    """SessionOut 的唯一构造入口：一次取齐这些会话的未读（通知领域）。"""
    unread = await c.notifications.unread_sessions([s.id for s in sessions])
    return [SessionOut.of(s, s.id in unread) for s in sessions]


class MailOut(BaseModel):
    """会话信箱里的一封信件（sessions.mailbox）。"""

    id: int
    kind: MailKind
    # 事件的来源（sessions.sources）；用户消息与回答为 null
    source: EventSource | None
    # 事件是运行中检查（工作还在进行）
    check_in: bool
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
            source=None if m.source is None else parse_source(m.source),
            check_in=m.check_in,
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


class LiveSegmentOut(BaseModel):
    segment_id: str
    events: list[RunEvent]

    @classmethod
    def of(cls, segment: LiveSegment) -> "LiveSegmentOut":
        return cls(segment_id=segment.segment_id, events=segment.events)


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
    # 正在进行的运行当前这一段的事件（与 SSE 的 run 事件同一形状，见 sessions.live.LiveSegments），让中途打开页面也能从这一段
    # 的开头看起；没有则为 null
    live: LiveSegmentOut | None
    # 工作状态（与会话列表同一条规则）：不是 idle 前端就显示停止。只剩挂起的提问时停止不清掉它（提问仍可回答）
    state: WorkState


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
    q: str | None = Query(
        None, min_length=1, description="关键字：只列标题或消息含有它的会话（不区分 ASCII 大小写）"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[SessionListItem]:
    """工作状态不是 idle 的会话（在全部符合筛选的会话里，不只是这一页）置顶，其余按最近更新排序。"""
    listed = await c.sessions.list(
        agent_id=agent_id, origins=origin, origin_ref=origin_ref, keyword=q, limit=limit, offset=offset
    )
    outs = await session_outs(c, [x.session for x in listed])
    return [SessionListItem(**o.model_dump(), state=x.state) for o, x in zip(outs, listed, strict=True)]


@router.post("/sessions", status_code=201)
async def start_session(c: C, body: StartIn) -> SessionOut:
    received = await c.sessions.receive(
        UserMessage(content=body.prompt, session=NewChat(body.agent_id), attachments=tuple(body.attachments))
    )
    [out] = await session_outs(c, [received.session])
    return out


@router.get("/sessions/{session_id}")
async def get_session(c: C, session_id: str) -> SessionDetail:
    view = await c.sessions.detail(session_id)
    session, *threads = await session_outs(c, [view.session, *view.threads])
    return SessionDetail(
        session=session,
        items=view.items,
        compactions=[CompactionOut.of(x) for x in view.compactions],
        mail=await mail_outs(c, view.mail),
        run=None if view.run is None else TaskOut.of(view.run),
        threads=threads,
        live=None if view.live is None else LiveSegmentOut.of(view.live),
        state=view.state,
    )


@router.post("/sessions/{session_id}/messages", status_code=202)
async def reply(c: C, session_id: str, body: ReplyIn) -> list[MailOut]:
    received = await c.sessions.receive(
        UserMessage(
            content=body.prompt, session=session_id, attachments=tuple(body.attachments), answers=body.answers
        )
    )
    return await mail_outs(c, received.mail)


@router.post("/sessions/{session_id}/stop", status_code=202, response_class=Response)
async def stop(c: C, session_id: str) -> None:
    """停止这个会话：结束正在进行的运行（已经产生的部分照常保存）、清空信箱，连带停下它发起的异步工作
    （docs/architecture.md「停止会话」）。"""
    await c.sessions.stop(session_id)


class CompactOut(BaseModel):
    # False 表示已有待执行的压缩，本次未重复投递
    queued: bool


@router.post("/sessions/{session_id}/compact", status_code=202)
async def compact(c: C, session_id: str) -> CompactOut:
    """手动压缩上下文（自动压缩见 sessions.compaction）：排在正在进行与排队的运行之后。"""
    return CompactOut(queued=await c.sessions.compact(session_id))


@router.post("/sessions/{session_id}/read", status_code=204)
async def mark_read(c: C, session_id: str) -> None:
    """标记会话已读；指向它的通知由通知领域随 SessionRead 标为已读。"""
    await c.sessions.mark_read(session_id)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(c: C, session_id: str) -> None:
    await c.sessions.delete(session_id)
