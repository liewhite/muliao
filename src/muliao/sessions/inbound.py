"""来信：一切投进会话信箱的东西的唯一入口（docs/architecture.md 第 14 节「触发源与通知」）。

- receive：用户消息、对提问的回答、委派来的任务，在调用方的事务里，投不进报领域错误；别的领域（触发器、issue）用
  SenderInbox.receive_in 在自己的事务里投信（同一套规则 check_in），得到结果：投进去了（Accepted：信进的会话）或投不进
  （Refused：原因由这里判断，发送方逐种决定自己的反应）。
- 目标会话三种：续写已有的、新建、按发送方定位（SessionKey：同一发送方给同一个 agent 的来信进同一个会话，没有就建）。
- 续写规则：用户可以在任何用户能写的会话里继续；其他来源只能续写自己发起的会话。
- 新建会话只有 create_session 一处实现（来信与 thread 都经过它）。
- 会话只为能运行的 agent 新建、继续：在本事务里用 agents 的 availability_in 检查，正在删除的按不存在处理（GONE），
  归档的拒绝（ARCHIVED）。agent 只会从可用变为正在删除、再变为不存在，没有先查后写的竞态：删除标记提交之后，它的
  会话不会再收到来信、变忙，删除流程清理会话时不必等待（docs/architecture.md 第 10 节）。归档只在 agent 空闲时做，
  归档之后来信进不来（agents.lifecycle）。"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import assert_never

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentArchived, Availability, agent_in, availability_in, runnable_agent_in
from muliao.errors import Conflict, CorruptState, Forbidden, InvalidInput, NotFound
from muliao.files.api import FileMeta, load_metas
from muliao.platform.db import utcnow
from muliao.platform.ids import new_id
from muliao.platform.tasks import TaskQueue
from muliao.sessions.approvals import ApprovalAnswered, Choice, pins_in
from muliao.sessions.directory import add_in, keyed_rows_among
from muliao.sessions.mailbox import open_asks_in, post_in
from muliao.sessions.origins import (
    RULES,
    DelegationOrigin,
    IssueOrigin,
    Origin,
    Sender,
    TriggerOrigin,
    UserOrigin,
    origin_of,
)
from muliao.sessions.pending import CallResult
from muliao.sessions.questions import QuestionAnswer, UserAnswered, answer_result
from muliao.sessions.tables import TITLE_LENGTH, Mail, MailKind, Session

log = logging.getLogger(__name__)

_TITLE_MAX = 60


class NewConversation(BaseModel):
    """别的领域的来信要新建的顶层会话，标题由发送方给出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    agent_id: str
    title: str


class SessionKey(BaseModel):
    """按发送方定位的会话：来信的发送方（SenderMail.sender）给这个 agent 的会话，没有就以 title 新建。发送方因此不用
    记会话 id（issue：每个 issue 与 agent 对应唯一一个会话）。有多个时（同一发送方新建过多个会话）取最新的。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    agent_id: str
    title: str


class SenderMail(BaseModel):
    """别的领域（触发器、issue）的来信，经 SenderInbox.receive_in 投递：只有文字，没有人在等（处理它的运行不推送）。
    sender 是具体的发送方（触发器、issue；构造时校验）；session 是投到哪个会话：新建的（触发器）、按发送方定位的（issue）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    sender: Sender
    content: str
    session: NewConversation | SessionKey


@dataclass(frozen=True)
class FromParent:
    """委派来的信：发送方是父会话（会话领域内部，对外的来源是 DelegationOrigin，父会话在 parent_session_id）。"""

    parent_session_id: str

    @property
    def kind(self) -> Origin:
        return Origin.DELEGATION


# 一封来信的具体发送方（会话领域内部）：用户、委派方（父会话）、别的领域的发送方
type From = UserOrigin | FromParent | TriggerOrigin | IssueOrigin


def default_title(text: str) -> str:
    """没给标题的新会话的标题：内容的第一行，过长时截断。"""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return _shorten(first, _TITLE_MAX)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class NewSession:
    """create_session 要新建的会话的描述。"""

    agent_id: str
    title: str
    # 委派产生的 thread：父会话与 thread 名
    parent_session_id: str | None = None
    thread: str | None = None


@dataclass(frozen=True)
class NewChat:
    """用户在界面上新建会话：标题由这条消息推导（文字的第一行，只发了文件时用文件名）。"""

    agent_id: str


@dataclass(frozen=True)
class Inbound:
    """一封来信的内部形式：用户消息、cron 到点、外部事件、agent 委派，都是「把 content 投进某个会话的信箱」（sessions.mailbox）。
    会话领域之外只用三种入口各自的类型：别的领域的 SenderMail、界面的 UserMessage；委派由 Threads.send 与 Delegations.dispatch 在领域内部构造。

    session 就是各入口自己的目标：已有会话的 id 时在其中继续；NewConversation（别的领域）、NewChat（用户）时新建会话；
    SessionKey 时按发送方定位（没有就新建）。sender 是具体的发送方：用户、委派方（父会话）、触发器、issue。
    """

    sender: From
    content: str
    session: str | NewConversation | SessionKey | NewChat
    # 用户是否在等它的回答（处理它的运行结束时推送）
    awaited: bool
    # 随消息发送的文件 id（muliao.files.store）；只有用户在界面上发的消息会带
    attachments: tuple[str, ...] = ()
    # 对这个会话里挂起提问的回答：tool_call_id -> 每题的回答（sessions.questions）；只有用户会带
    answers: Mapping[str, Sequence[QuestionAnswer]] = field(default_factory=dict)
    # 对这个会话里等批准的调用的决定：tool_call_id -> 批准、拒绝或始终允许（sessions.approvals）；只有用户会带
    approvals: Mapping[str, Choice] = field(default_factory=dict)
    # 委派来的信件：是哪项委派（sessions.thread_calls 的行 id），结局按它交回；只有 Delegations.dispatch 会带
    delegation: int | None = None


def _inbound_of_mail(mail: SenderMail) -> Inbound:
    """别的领域的来信：没有人在等。"""
    return Inbound(
        sender=mail.sender,
        content=mail.content,
        session=mail.session,
        awaited=False,
    )


@dataclass(frozen=True)
class UserMessage:
    """用户在界面上发的一条消息：续写会话或新建会话，可以带附件与对挂起提问的回答；用户在等它的回答。"""

    content: str
    session: str | NewChat
    # 随消息发送的文件 id（muliao.files.store）
    attachments: tuple[str, ...] = ()
    # 对这个会话里挂起提问的回答：tool_call_id -> 每题的回答（sessions.questions）
    answers: Mapping[str, Sequence[QuestionAnswer]] = field(default_factory=dict)
    # 对这个会话里等批准的调用的决定：tool_call_id -> 批准、拒绝或始终允许（sessions.approvals）
    approvals: Mapping[str, Choice] = field(default_factory=dict)

    def inbound(self) -> Inbound:
        return Inbound(
            sender=UserOrigin(),
            content=self.content,
            session=self.session,
            awaited=True,
            attachments=self.attachments,
            answers=self.answers,
            approvals=self.approvals,
        )


@dataclass(frozen=True)
class Received:
    session: Session
    # 投进信箱的信件（回答一封、文字或附件一封）
    mail: list[Mail]


def _sender_of(session: Session) -> From:
    """会话的发起方。委派会话的发起方就是它的父会话（存在 parent_session_id，不另存副本）。"""
    match origin := origin_of(session):
        case DelegationOrigin():
            if session.parent_session_id is None:
                raise CorruptState(f"delegated session {session.id} has no parent")
            return FromParent(session.parent_session_id)
        case UserOrigin() | TriggerOrigin() | IssueOrigin():
            return origin
        case other:
            assert_never(other)


def _may_continue(session: Session, sender: From) -> bool:
    """续写规则：用户可以在任何会话里继续；其他来源只能续写自己发起的会话。"""
    return RULES[sender.kind].writes_anywhere or _sender_of(session) == sender


async def create_session(s: AsyncSession, *, sender: From, spec: NewSession) -> Session:
    """新建会话的唯一实现（receive 与 ask_agent 占用 thread 都经过这里）。"""
    await runnable_agent_in(s, spec.agent_id)
    is_thread = isinstance(sender, FromParent)
    if (spec.parent_session_id is not None) != is_thread or (spec.thread is not None) != is_thread:
        raise ValueError(
            f"only delegation creates threads, each with a parent and a name; got {sender.kind} creating a "
            f"session with parent {spec.parent_session_id} and name {spec.thread!r}"
        )
    if isinstance(sender, FromParent) and spec.parent_session_id != sender.parent_session_id:
        raise ValueError(
            f"delegated session must be created by its parent {spec.parent_session_id}, "
            f"not {sender.parent_session_id}"
        )
    return await add_in(
        s,
        session_id=new_id(),
        agent_id=spec.agent_id,
        # 发送方给的标题（如 issue 标题）可能比列宽长，在这里统一截断
        title=_shorten(spec.title, TITLE_LENGTH),
        origin=sender.kind,
        # 只有别的领域的发送方在库里有编号；委派的发起方即 parent_session_id
        origin_ref=sender.ref if isinstance(sender, TriggerOrigin | IssueOrigin) else None,
        parent_session_id=spec.parent_session_id,
        thread=spec.thread,
    )


async def _keyed_session_in(s: AsyncSession, inbound: Inbound, agent_id: str) -> Session | None:
    """按发送方定位的会话（SessionKey）：这个发送方给这个 agent 开过的最新一个顶层会话（规则在 directory.keyed_rows_among）。"""
    sender = inbound.sender
    if not isinstance(sender, TriggerOrigin | IssueOrigin):
        raise ValueError(f"only a sender with an identity can address a session by key, got {sender.kind}")
    return (await keyed_rows_among(s, [sender], agent_id)).get((sender, agent_id))


async def _resolve_answers(
    s: AsyncSession,
    session_id: str,
    answers: Mapping[str, Sequence[QuestionAnswer]],
    approvals: Mapping[str, Choice],
) -> dict[str, CallResult]:
    """用户提交的回答与批准决定：每个都对应还在等的提问、等批准的调用。返回回答信件里存的结果。"""
    asks = await open_asks_in(s, session_id)
    resolved: dict[str, CallResult] = {}
    for call_id, given in answers.items():
        if call_id not in asks.answerable:
            raise Conflict(f"question {call_id!r} is not waiting for an answer in session {session_id}")
        resolved[call_id] = UserAnswered.of(answer_result(asks.answerable[call_id], given))
    pins = await pins_in(s, session_id, list(approvals)) if approvals else {}
    for call_id, choice in approvals.items():
        if call_id not in asks.approvable:
            raise Conflict(f"call {call_id!r} is not waiting for approval in session {session_id}")
        resolved[call_id] = ApprovalAnswered.of(choice, pins[call_id])
    return resolved


class Refusal(StrEnum):
    """一封来信投不进的原因（只有收信入口判断，发送方按原因决定自己怎么反应）。"""

    # 目标 agent 已归档：取消归档后又能投
    ARCHIVED = "archived"
    # 目标会话已删除，或目标 agent 不存在、正在删除
    GONE = "gone"
    # 目标会话不是这个发送方开的，它不准续写
    NOT_YOURS = "not_yours"


@dataclass(frozen=True)
class Refused:
    """投不进：原因与给人看的说明。"""

    reason: Refusal
    detail: str


@dataclass(frozen=True)
class Checked:
    """校验通过的来信：要续写的会话（新建时为 None）与附件的元数据（推导标题时用，不再读一遍）。"""

    session: Session | None
    attachments: list[FileMeta]


async def agent_refusal_in(s: AsyncSession, agent_id: str) -> Refused | None:
    """新来信能不能交给这个 agent（不能时是原因，check_in 用）。"""
    match await availability_in(s, agent_id):
        case Availability.RUNNABLE:
            return None
        case Availability.ARCHIVED:
            name = (await agent_in(s, agent_id)).name
            return Refused(Refusal.ARCHIVED, f"agent {name!r} is archived; unarchive it first")
        case Availability.GONE:
            return Refused(Refusal.GONE, f"agent {agent_id!r} not found")
        case other:
            assert_never(other)


async def check_in(s: AsyncSession, inbound: Inbound) -> Checked | Refused:
    """来信的校验规则（只读）：目标会话存在且发送方可以续写、agent 能运行（没在删除、没归档），不满足的是 Refused。
    内容为空、附件不存在是发送方自己的错（只有用户的消息会碰到），仍然报错。"""
    if (
        not inbound.content.strip()
        and not inbound.attachments
        and not inbound.answers
        and not inbound.approvals
    ):
        raise InvalidInput("message must not be empty")
    attachments = await load_metas(s, inbound.attachments)
    if isinstance(inbound.session, (NewConversation, NewChat)):
        refused = await agent_refusal_in(s, inbound.session.agent_id)
        return refused or Checked(None, attachments)
    if isinstance(inbound.session, SessionKey):
        key = inbound.session
        session = await _keyed_session_in(s, inbound, key.agent_id)
        if session is None:
            refused = await agent_refusal_in(s, key.agent_id)
            return refused or Checked(None, attachments)
    else:
        session = await s.get(Session, inbound.session)
        if session is None:
            return Refused(Refusal.GONE, f"session {inbound.session!r} not found")
    if not _may_continue(session, inbound.sender):
        log.warning(
            "continuation refused",
            extra={"session_id": session.id, "sender": repr(inbound.sender)},
        )
        return Refused(
            Refusal.NOT_YOURS,
            f"session {session.id} was not started by this sender; it may only continue its own sessions",
        )
    refused = await agent_refusal_in(s, session.agent_id)
    return refused or Checked(session, attachments)


async def _target_in(s: AsyncSession, inbound: Inbound) -> Checked:
    """用户与委派的来信（调用方在等结果、投不进就是这次操作失败）：投不进按原因报领域错误。"""
    match checked := await check_in(s, inbound):
        case Checked():
            return checked
        case Refused(reason=Refusal.ARCHIVED, detail=detail):
            raise AgentArchived(detail)
        case Refused(reason=Refusal.GONE, detail=detail):
            raise NotFound(detail)
        case Refused(reason=Refusal.NOT_YOURS, detail=detail):
            raise Forbidden(detail)
        case Refused(reason=other):
            assert_never(other)


@dataclass(frozen=True)
class Accepted:
    """投进去了：信进的会话。"""

    session_id: str


class SenderInbox:
    """别的领域（触发器、issue）投信的唯一入口，在发送方的事务里：发送方记下的状态与投信一起提交。"""

    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def receive_in(self, s: AsyncSession, mail: SenderMail) -> Accepted | Refused:
        """投不进的返回 Refused（原因由这里判断，发送方只决定自己怎么反应）。"""
        inbound = _inbound_of_mail(mail)
        match checked := await check_in(s, inbound):
            case Refused():
                return checked
            case Checked():
                received = await receive_into(s, self._queue, inbound, checked)
                return Accepted(received.session.id)
            case other:
                assert_never(other)


def _derived_title(inbound: Inbound, attachments: Sequence[FileMeta]) -> str:
    """用户新建会话（NewChat）的标题：文字的第一行；只发了文件时用文件名。"""
    if inbound.content.strip():
        return default_title(inbound.content)
    return default_title(", ".join(m.name for m in attachments))


async def receive(s: AsyncSession, queue: TaskQueue, inbound: Inbound) -> Received:
    """所有来信的唯一入口：确定会话（继续或新建），把它投进会话的信箱。"""
    return await receive_into(s, queue, inbound, await _target_in(s, inbound))


async def receive_into(s: AsyncSession, queue: TaskQueue, inbound: Inbound, checked: Checked) -> Received:
    """把已经校验过的来信（check_in 的结果）投进会话的信箱。只给要先单独处理校验结果的调用方（SenderInbox）；其余一律
    经 receive。"""
    session, attachments = checked.session, checked.attachments
    if session is None:
        match inbound.session:
            case str(session_id):
                raise RuntimeError(f"inbound for session {session_id!r} resolved to no session")
            case SessionKey(agent_id=agent_id, title=title):
                spec = NewSession(agent_id=agent_id, title=title)
            case NewConversation(agent_id=agent_id, title=title):
                spec = NewSession(agent_id=agent_id, title=title)
            case NewChat(agent_id=agent_id):
                spec = NewSession(agent_id=agent_id, title=_derived_title(inbound, attachments))
            case other:
                assert_never(other)
        session = await create_session(s, sender=inbound.sender, spec=spec)
    session.updated_at = utcnow()
    mail = []
    if inbound.answers or inbound.approvals:
        answers = await _resolve_answers(s, session.id, inbound.answers, inbound.approvals)
        mail.append(
            await post_in(
                s,
                queue,
                session.id,
                kind=MailKind.ANSWER,
                origin=inbound.sender.kind,
                content="",
                answers={k: v.model_dump() for k, v in answers.items()},
                awaited=inbound.awaited,
            )
        )
    if inbound.content.strip() or inbound.attachments:
        mail.append(
            await post_in(
                s,
                queue,
                session.id,
                kind=MailKind.MESSAGE,
                origin=inbound.sender.kind,
                content=inbound.content,
                attachments=inbound.attachments,
                awaited=inbound.awaited,
                delegation=inbound.delegation,
            )
        )
    log.info(
        "message received",
        extra={"session_id": session.id, "sender": repr(inbound.sender)},
    )
    return Received(session, mail)
