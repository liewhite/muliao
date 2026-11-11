"""来信：一切投进会话信箱的东西的唯一入口（docs/architecture.md 第 14 节「触发源与通知」）。

- receive：用户消息、对提问的回答、委派来的任务，在调用方的事务里；别的领域（触发器、issue）用 receive_command_in 在
  自己的事务里校验（同一套规则，只读）、得到定向命令 Receive 发出，由会话领域在自己的事务里投信。
- 目标会话三种：续写已有的、新建、按发送方定位（SessionKey：同一发送方给同一个 agent 的来信进同一个会话，没有就建）。
- 续写规则：用户可以在任何用户能写的会话里继续；其他来源只能续写自己发起的会话。
- 新建会话只有 create_session 一处实现（来信与 thread 都经过它）。
- 会话只为能运行的 agent 新建、继续：在本事务里用 agents 的 runnable_agent_in 检查，正在删除的按不存在处理（NotFound），
  归档的拒绝（AgentArchived）。agent 只会从可用变为正在删除、再变为不存在，没有先查后写的竞态：删除标记提交之后，它的
  会话不会再收到来信、变忙，删除流程清理会话时不必等待（docs/architecture.md 第 10 节）。归档只在 agent 空闲时做，
  归档之后来信进不来（agents.lifecycle）。"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import assert_never

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import runnable_agent_in
from muliao.config import MIN_COMPACTION_THRESHOLD_TOKENS
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.files.api import FileMeta, load_metas
from muliao.platform.db import utcnow
from muliao.platform.ids import new_id
from muliao.platform.tasks import TaskQueue
from muliao.sessions.approvals import ApprovalAnswered, Choice, pins_in
from muliao.sessions.directory import add_in, keyed_rows_among
from muliao.sessions.events import NewConversation, Receive, SenderMail, SessionKey
from muliao.sessions.lanes import keyed_receive_lane, receive_lane
from muliao.sessions.mailbox import open_asks_in, post_in
from muliao.sessions.origins import RULES, Origin
from muliao.sessions.pending import CallResult
from muliao.sessions.questions import QuestionAnswer, UserAnswered, answer_result
from muliao.sessions.tables import TITLE_LENGTH, Mail, MailKind, Session

log = logging.getLogger(__name__)

_TITLE_MAX = 60


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
    # 发送方预先分配的会话 id（NewConversation）；None 时生成
    id: str | None = None
    # 这个会话自己的压缩阈值（token）；None 用全局默认。只有用户在界面上新建会话（NewChat）会给
    compaction_threshold_tokens: int | None = None


@dataclass(frozen=True)
class NewChat:
    """用户在界面上新建会话：标题由这条消息推导（文字的第一行，只发了文件时用文件名）。"""

    agent_id: str
    # 这个会话自己的压缩阈值（token）；None 用全局默认
    compaction_threshold_tokens: int | None = None


@dataclass(frozen=True)
class Inbound:
    """一封来信的内部形式：用户消息、cron 到点、外部事件、agent 委派，都是「把 content 投进某个会话的信箱」（sessions.mailbox）。
    会话领域之外只用三种入口各自的类型：别的领域的 SenderMail、界面的 UserMessage；委派由 Threads.send 与 Delegations.dispatch 在领域内部构造。

    session 就是各入口自己的目标：已有会话的 id 时在其中继续；NewConversation（别的领域）、NewChat（用户）时新建会话；
    SessionKey 时按发送方定位（没有就新建）。origin_ref 标识具体的发送方：触发器 id、issue id、委派方（父）会话 id；用户为 None。
    """

    origin: Origin
    origin_ref: str | None
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
    # 委派来的信件：关联 id（发起方的占用者），结局按它交回；只有 Delegations.dispatch 会带
    correlation: str | None = None


def _inbound_of_mail(mail: SenderMail) -> Inbound:
    """别的领域的来信：没有人在等。"""
    return Inbound(
        origin=mail.origin,
        origin_ref=mail.origin_ref,
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
            origin=Origin.USER,
            origin_ref=None,
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


def _sender_of(session: Session) -> tuple[Origin, str | None]:
    """会话的发起方。委派会话的发起方就是它的父会话（存在 parent_session_id，不另存副本）。"""
    if RULES[session.origin].from_parent:
        return session.origin, session.parent_session_id
    return session.origin, session.origin_ref


def _check_may_continue(session: Session, origin: Origin, origin_ref: str | None) -> None:
    """续写规则：用户可以在任何会话里继续；其他来源只能续写自己发起的会话。"""
    if RULES[origin].writes_anywhere:
        return
    if _sender_of(session) != (origin, origin_ref):
        log.warning(
            "continuation refused",
            extra={"session_id": session.id, "origin": origin, "origin_ref": origin_ref},
        )
        raise Forbidden(
            f"session {session.id} was not started by this sender; it may only continue its own sessions"
        )


async def create_session(
    s: AsyncSession, *, origin: Origin, origin_ref: str | None, spec: NewSession
) -> Session:
    """新建会话的唯一实现（receive 与 ask_agent 占用 thread 都经过这里）。"""
    await runnable_agent_in(s, spec.agent_id)
    is_thread = RULES[origin].from_parent
    if (spec.parent_session_id is not None) != is_thread or (spec.thread is not None) != is_thread:
        raise ValueError(
            f"only delegation creates threads, each with a parent and a name; got {origin} creating a "
            f"session with parent {spec.parent_session_id} and name {spec.thread!r}"
        )
    if is_thread and spec.parent_session_id != origin_ref:
        raise ValueError(
            f"delegated session must be created by its parent {spec.parent_session_id}, not {origin_ref}"
        )
    threshold = spec.compaction_threshold_tokens
    if threshold is not None and threshold < MIN_COMPACTION_THRESHOLD_TOKENS:
        raise InvalidInput(
            f"compaction threshold must be at least {MIN_COMPACTION_THRESHOLD_TOKENS} tokens, got {threshold}"
        )
    return await add_in(
        s,
        session_id=new_id() if spec.id is None else spec.id,
        agent_id=spec.agent_id,
        # 发送方给的标题（如 issue 标题）可能比列宽长，在这里统一截断
        title=_shorten(spec.title, TITLE_LENGTH),
        origin=origin,
        # 委派的发起方即 parent_session_id
        origin_ref=None if is_thread else origin_ref,
        parent_session_id=spec.parent_session_id,
        thread=spec.thread,
        compaction_threshold_tokens=spec.compaction_threshold_tokens,
    )


async def keyed_session_in(s: AsyncSession, origin: Origin, origin_ref: str, agent_id: str) -> Session | None:
    """按发送方定位的会话（SessionKey）：这个发送方给这个 agent 开过的最新一个顶层会话（规则在 directory.keyed_rows_among）。"""
    return (await keyed_rows_among(s, origin, [origin_ref], agent_id)).get((origin_ref, agent_id))


def _key_ref(inbound: Inbound) -> str:
    if not RULES[inbound.origin].identified or inbound.origin_ref is None:
        raise ValueError(f"only a sender with an identity can address a session by key, got {inbound.origin}")
    return inbound.origin_ref


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


async def target_in(s: AsyncSession, inbound: Inbound) -> tuple[Session | None, list[FileMeta]]:
    """来信的校验规则（只读）：内容不为空、附件存在、目标会话存在且发送方可以续写、agent 能运行（没在删除、没归档）。返回要续写的会话
    （新建时为 None）与附件的元数据（推导标题时用，不再读一遍）。"""
    if (
        not inbound.content.strip()
        and not inbound.attachments
        and not inbound.answers
        and not inbound.approvals
    ):
        raise InvalidInput("message must not be empty")
    attachments = await load_metas(s, inbound.attachments)
    if isinstance(inbound.session, (NewConversation, NewChat)):
        await runnable_agent_in(s, inbound.session.agent_id)
        return None, attachments
    if isinstance(inbound.session, SessionKey):
        key = inbound.session
        session = await keyed_session_in(s, inbound.origin, _key_ref(inbound), key.agent_id)
        if session is None:
            await runnable_agent_in(s, key.agent_id)
            return None, attachments
    else:
        session = await s.get(Session, inbound.session)
        if session is None:
            raise NotFound(f"session {inbound.session!r} not found")
    _check_may_continue(session, inbound.origin, inbound.origin_ref)
    await runnable_agent_in(s, session.agent_id)
    return session, attachments


def receive_lane_of(command: Receive) -> str:
    """Receive 投递所在的 lane：同一个目标的来信按发送顺序投递。按 key 定位的目标在会话存在之前就要有 lane。"""
    mail = command.mail
    match mail.session:
        case SessionKey(agent_id=agent_id):
            return keyed_receive_lane(mail.origin, mail.origin_ref, agent_id)
        case NewConversation(id=session_id) | str(session_id):
            return receive_lane(session_id)
        case other:
            assert_never(other)


async def receive_command_in(s: AsyncSession, mail: SenderMail) -> Receive:
    """别的领域投信的唯一入口（只读）：在发送方的事务里校验这封来信（不合规的当场报错给发送方），返回定向命令
    Receive。发送方在同一事务里 send_in 它，由会话领域在自己的事务里投进信箱——发送方记下的状态与投信一起提交，
    投递恰好一次；提交后由 Messaging 马上执行投递。校验读的都是只会单向变化或不变的事实（会话与 agent
    是否还在、会话的发起方、文件），投递时只剩「期间被删除」这一种契约内的变化（docs/architecture.md 2.2）。"""
    await target_in(s, _inbound_of_mail(mail))
    return Receive(mail=mail)


def inbound_of(command: Receive) -> Inbound:
    return _inbound_of_mail(command.mail)


def _derived_title(inbound: Inbound, attachments: Sequence[FileMeta]) -> str:
    """用户新建会话（NewChat）的标题：文字的第一行；只发了文件时用文件名。"""
    if inbound.content.strip():
        return default_title(inbound.content)
    return default_title(", ".join(m.name for m in attachments))


async def receive(s: AsyncSession, queue: TaskQueue, inbound: Inbound) -> Received:
    """所有来信的唯一入口：确定会话（继续或新建），把它投进会话的信箱。"""
    return await receive_into(s, queue, inbound, *await target_in(s, inbound))


async def receive_into(
    s: AsyncSession,
    queue: TaskQueue,
    inbound: Inbound,
    session: Session | None,
    attachments: Sequence[FileMeta],
) -> Received:
    """把已经校验过的来信（target_in 的结果：要续写的会话，新建时为 None；附件的元数据）投进会话的信箱。只给要先
    单独处理校验结果的调用方（Receive 的投递）；其余一律经 receive。"""
    if session is None:
        match inbound.session:
            case str(session_id):
                raise RuntimeError(f"inbound for session {session_id!r} resolved to no session")
            case SessionKey(agent_id=agent_id, title=title):
                spec = NewSession(agent_id=agent_id, title=title)
            case NewConversation(id=session_id, agent_id=agent_id, title=title):
                spec = NewSession(agent_id=agent_id, title=title, id=session_id)
            case NewChat(agent_id=agent_id, compaction_threshold_tokens=threshold):
                spec = NewSession(
                    agent_id=agent_id,
                    title=_derived_title(inbound, attachments),
                    compaction_threshold_tokens=threshold,
                )
            case other:
                assert_never(other)
        session = await create_session(s, origin=inbound.origin, origin_ref=inbound.origin_ref, spec=spec)
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
                origin=inbound.origin,
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
                origin=inbound.origin,
                content=inbound.content,
                attachments=inbound.attachments,
                awaited=inbound.awaited,
                correlation=inbound.correlation,
            )
        )
    log.info(
        "message received",
        extra={"session_id": session.id, "origin": inbound.origin, "origin_ref": inbound.origin_ref},
    )
    return Received(session, mail)
