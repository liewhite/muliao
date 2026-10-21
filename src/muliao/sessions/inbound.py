"""来信：一切投进会话信箱的东西的唯一入口（docs/architecture.md 第 14 节「触发源与通知」）。

- receive：用户消息、对提问的回答、委派来的任务，在调用方的事务里；别的领域（触发器、issue）用 receive_command_in 在
  自己的事务里校验（同一套规则，只读）、得到定向命令 Receive 发出，由会话领域在自己的事务里投信。
- 目标会话三种：续写已有的、新建、按发送方定位（SessionKey：同一发送方给同一个 agent 的来信进同一个会话，没有就建）。
- 续写规则：用户可以在任何用户能写的会话里继续；其他来源只能续写自己发起的会话。
- 新建会话只有 create_session 一处实现（来信与 thread 都经过它）。"""

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from pydantic_ai.messages import ModelMessage
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import active_agent_in
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.files.api import FileMeta, load_metas
from muliao.platform.db import utcnow
from muliao.platform.ids import new_id
from muliao.platform.tasks import TaskQueue
from muliao.sessions.events import Receive, ReceiveKey, ReceiveNewSession
from muliao.sessions.history import since_last_response, since_last_response_among
from muliao.sessions.info import keyed_rows_among
from muliao.sessions.kinds import SessionKind, policy_of
from muliao.sessions.lanes import keyed_receive_lane, receive_lane
from muliao.sessions.mailbox import post_in, queued_among, queued_in
from muliao.sessions.origins import Origin
from muliao.sessions.pending import UserAnswered, pending_questions
from muliao.sessions.questions import AskResult, Question, QuestionAnswer, answer_result
from muliao.sessions.tables import Mail, MailKind, Session

log = logging.getLogger(__name__)

_TITLE_MAX = 60


def default_title(text: str) -> str:
    """没给标题的新会话的标题：内容的第一行，过长时截断。"""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first if len(first) <= _TITLE_MAX else first[: _TITLE_MAX - 1] + "…"


class DerivedTitle(Enum):
    """新会话的标题不由发送方给出，而由 receive 按这封来信推导（default_title：文字的第一行，只有附件时用文件名）。
    只有同步投信（receive）接受它：用户在界面上新建会话时没有标题；别的入口（定向命令 Receive、thread）必须给出。"""

    FROM_MESSAGE = "from_message"


@dataclass(frozen=True)
class NewSession:
    """事件要求新建会话时的会话描述。"""

    agent_id: str
    title: str | DerivedTitle
    # 委派产生的 thread：父会话、thread 名与种类
    parent_session_id: str | None = None
    thread: str | None = None
    kind: SessionKind = SessionKind.CONVERSATION
    # 发送方预先分配的会话 id（经定向命令异步投递时，发送方要立即告诉它的调用方会话 id，见 Receive）；None 时生成
    id: str | None = None


@dataclass(frozen=True)
class SessionKey:
    """按发送方定位的会话：来信的发送方（origin, origin_ref）给这个 agent 的会话，没有就以 title 新建。发送方因此不用
    记会话 id（issue：每个 issue 与 agent 对应唯一一个会话）。有多个时（同一发送方新建过多个会话）取最新的。"""

    agent_id: str
    title: str


@dataclass(frozen=True)
class Inbound:
    """一封来信：用户消息、cron 到点、外部事件、agent 委派，都是「把 content 投进某个会话的信箱」（sessions.mailbox）。

    session 为已有会话的 id 时在其中继续，为 NewSession 时新建会话，为 SessionKey 时按发送方定位（没有就新建）。
    origin_ref 标识具体的发送方：触发器 id、委派方（父）会话 id；用户为 None。
    """

    origin: Origin
    origin_ref: str | None
    content: str
    session: str | NewSession | SessionKey
    # 用户是否在等它的回答（处理它的运行结束时推送）
    awaited: bool
    # 随消息发送的文件 id（muliao.files.store）；只有用户在界面上发的消息会带
    attachments: tuple[str, ...] = ()
    # 对这个会话里挂起提问的回答：tool_call_id -> 每题的回答（sessions.questions）；只有用户会带
    answers: Mapping[str, Sequence[QuestionAnswer]] = field(default_factory=dict)
    # 委派来的信件：关联 id（发起方的占用者），结局按它交回；只有 Threads.dispatch 会带
    correlation: str | None = None


@dataclass(frozen=True)
class Received:
    session: Session
    # 投进信箱的信件（回答一封、文字或附件一封）
    mail: list[Mail]


def _sender_of(session: Session) -> tuple[Origin, str | None]:
    """会话的发起方。委派会话的发起方就是它的父会话（存在 parent_session_id，不另存副本）。"""
    if session.origin == Origin.DELEGATION:
        return session.origin, session.parent_session_id
    return session.origin, session.origin_ref


def _check_may_continue(session: Session, origin: Origin, origin_ref: str | None) -> None:
    """续写规则：用户可以在任何非私有会话里继续；其他来源只能续写自己发起的会话。"""
    if origin == Origin.USER:
        if not policy_of(session.kind).user_writable:
            raise Conflict(f"session {session.id} is a {session.kind} of another agent and is read-only")
        return
    if _sender_of(session) != (origin, origin_ref):
        log.warning(
            "continuation refused",
            extra={"session_id": session.id, "origin": origin, "origin_ref": origin_ref},
        )
        raise Forbidden(
            f"session {session.id} was not started by this sender; it may only continue its own sessions"
        )


async def ensure_agent_active_in(s: AsyncSession, agent_id: str) -> None:
    """会话只为还可用的 agent 新建、继续：正在删除的按不存在处理（agents 的 active_agent_in）。agent 只会从可用变为
    正在删除、再变为不存在，在本事务里检查没有先查后写的竞态：删除标记提交之后，它的会话不会再收到来信、变忙，
    删除流程清理会话时不必等待（docs/architecture.md 第 10 节）。"""
    await active_agent_in(s, agent_id)


async def create_session(
    s: AsyncSession, *, origin: Origin, origin_ref: str | None, spec: NewSession
) -> Session:
    """新建会话的唯一实现（receive 与 ask_agent 占用 thread 都经过这里）。"""
    if not isinstance(spec.title, str):
        raise ValueError(f"a new session needs a title by now (receive derives one), got {spec}")
    await ensure_agent_active_in(s, spec.agent_id)
    is_thread = origin == Origin.DELEGATION
    if (
        (spec.parent_session_id is not None) != is_thread
        or policy_of(spec.kind).opened_by_delegation != is_thread
        or (spec.thread is not None) != is_thread
    ):
        raise ValueError(
            f"only delegation creates threads, each with a parent and a name; got {origin} creating a "
            f"{spec.kind} with parent {spec.parent_session_id} and name {spec.thread!r}"
        )
    if origin == Origin.DELEGATION and spec.parent_session_id != origin_ref:
        raise ValueError(
            f"delegated session must be created by its parent {spec.parent_session_id}, not {origin_ref}"
        )
    session = Session(
        id=new_id() if spec.id is None else spec.id,
        agent_id=spec.agent_id,
        title=spec.title,
        origin=origin,
        # 委派的发起方即 parent_session_id
        origin_ref=None if origin == Origin.DELEGATION else origin_ref,
        parent_session_id=spec.parent_session_id,
        thread=spec.thread,
        kind=spec.kind,
    )
    s.add(session)
    await s.flush()
    return session


async def keyed_session_in(s: AsyncSession, origin: Origin, origin_ref: str, agent_id: str) -> Session | None:
    """按发送方定位的会话（SessionKey）：这个发送方给这个 agent 开过的最新一个顶层会话（规则在 info.keyed_rows_among）。"""
    return (await keyed_rows_among(s, origin, [origin_ref], agent_id)).get((origin_ref, agent_id))


def _key_ref(inbound: Inbound) -> str:
    if inbound.origin in (Origin.USER, Origin.DELEGATION) or inbound.origin_ref is None:
        raise ValueError(f"only a sender with an identity can address a session by key, got {inbound.origin}")
    return inbound.origin_ref


@dataclass(frozen=True)
class OpenAsks:
    """还没有结果的提问分成两种：现在能回答的（answerable），与回答已提交、还在信箱里排队的（submitted）。
    回答在交给模型之前一直排着（运行失败重试时也还在），所以不需要别的条件。界面展示与投信校验都经过 open_asks。"""

    answerable: dict[str, list[Question]]
    submitted: frozenset[str]


async def open_asks(s: AsyncSession, session_id: str) -> OpenAsks:
    return _open_asks(await since_last_response(s, session_id), await queued_in(s, session_id))


async def open_asks_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, OpenAsks]:
    """open_asks 的批量版本（会话列表的工作状态），同一条规则。"""
    tails = await since_last_response_among(s, session_ids)
    queued = await queued_among(s, session_ids)
    return {x: _open_asks(tails[x], queued[x]) for x in session_ids}


def _open_asks(since_last_response: Sequence[ModelMessage], queued: Sequence[Mail]) -> OpenAsks:
    """挂起的提问只可能在最后一条模型回复里（history.since_last_response）；回答已在信箱里排着的算已提交。"""
    pending = pending_questions(since_last_response)
    submitted = frozenset(k for m in queued if m.kind == MailKind.ANSWER for k in m.answers)
    return OpenAsks(answerable={k: v for k, v in pending.items() if k not in submitted}, submitted=submitted)


async def _resolve_answers(
    s: AsyncSession, session_id: str, answers: Mapping[str, Sequence[QuestionAnswer]]
) -> dict[str, AskResult]:
    """用户提交的回答：每个都对应还在等回答的提问。返回交给模型的结果。"""
    pending = (await open_asks(s, session_id)).answerable
    resolved = {}
    for call_id, given in answers.items():
        if call_id not in pending:
            raise Conflict(f"question {call_id!r} is not waiting for an answer in session {session_id}")
        resolved[call_id] = answer_result(pending[call_id], given)
    return resolved


async def _target_in(s: AsyncSession, inbound: Inbound) -> tuple[Session | None, list[FileMeta]]:
    """来信的校验规则（只读）：内容不为空、附件存在、目标会话存在且发送方可以续写、agent 还可用。返回要续写的会话
    （新建时为 None）与附件的元数据（推导标题时用，不再读一遍）。"""
    if not inbound.content.strip() and not inbound.attachments and not inbound.answers:
        raise InvalidInput("message must not be empty")
    attachments = await load_metas(s, inbound.attachments)
    if isinstance(inbound.session, NewSession):
        await ensure_agent_active_in(s, inbound.session.agent_id)
        return None, attachments
    if isinstance(inbound.session, SessionKey):
        key = inbound.session
        session = await keyed_session_in(s, inbound.origin, _key_ref(inbound), key.agent_id)
        if session is None:
            await ensure_agent_active_in(s, key.agent_id)
            return None, attachments
    else:
        session = await s.get(Session, inbound.session)
        if session is None:
            raise NotFound(f"session {inbound.session!r} not found")
    _check_may_continue(session, inbound.origin, inbound.origin_ref)
    await ensure_agent_active_in(s, session.agent_id)
    return session, attachments


async def check_receivable_in(s: AsyncSession, inbound: Inbound) -> None:
    """在发送方的事务里校验一封来信（不写入），之后经定向命令 Receive 投递。校验读的都是只会单向变化或不变的事实
    （会话与 agent 是否还在、会话的发起方、文件），投递时只剩「期间被删除」这一种契约内的变化（docs/architecture.md
    2.2）。"""
    await _target_in(s, inbound)


def receive_lane_of(command: Receive) -> str:
    """Receive 投递所在的 lane：同一个目标的来信按发送顺序投递。按 key 定位的目标在会话存在之前就要有 lane。"""
    if command.key is not None:
        if command.origin_ref is None:
            raise ValueError(f"a keyed Receive needs the sender's identity, got {command}")
        return keyed_receive_lane(command.origin, command.origin_ref, command.key.agent_id)
    if command.new_session is not None:
        return receive_lane(command.new_session.id)
    if command.session_id is None:
        raise ValueError(f"Receive needs a target session, got {command}")
    return receive_lane(command.session_id)


def _receive_command(inbound: Inbound) -> Receive:
    """一封要经定向命令投递的来信。新建会话必须带预先分配的 id；只有文字内容的来信才能这样投（附件、回答、关联只来自
    同步的入口）。"""
    if inbound.attachments or inbound.answers or inbound.correlation is not None:
        raise ValueError("only plain text inbound mail can be sent as a Receive command")
    target = inbound.session
    session_id = new_session = key = None
    if isinstance(target, str):
        session_id = target
    elif isinstance(target, SessionKey):
        _key_ref(inbound)
        key = ReceiveKey(agent_id=target.agent_id, title=target.title)
    else:
        if target.id is None or policy_of(target.kind).opened_by_delegation:
            raise ValueError(
                f"a Receive command creates a top-level session with a preassigned id, got {target}"
            )
        if not isinstance(target.title, str):
            raise ValueError(
                f"a Receive command creates a session with a title given by its sender, got {target}"
            )
        new_session = ReceiveNewSession(id=target.id, agent_id=target.agent_id, title=target.title)
    return Receive(
        origin=inbound.origin,
        origin_ref=inbound.origin_ref,
        content=inbound.content,
        awaited=inbound.awaited,
        session_id=session_id,
        new_session=new_session,
        key=key,
    )


async def receive_command_in(s: AsyncSession, inbound: Inbound) -> Receive:
    """别的领域投信的唯一入口（只读）：在发送方的事务里校验这封来信（不合规的当场报错给发送方），返回定向命令
    Receive。发送方在同一事务里 send_in 它，由会话领域在自己的事务里投进信箱——发送方记下的状态与投信一起提交，
    投递恰好一次；提交后可以让投递任务马上执行（run_now）。"""
    await check_receivable_in(s, inbound)
    return _receive_command(inbound)


def inbound_of(command: Receive) -> Inbound:
    targets = [x for x in (command.session_id, command.new_session, command.key) if x is not None]
    if len(targets) != 1:
        raise ValueError(f"Receive needs exactly one target session, got {command}")
    session: str | NewSession | SessionKey
    if command.new_session is not None:
        new = command.new_session
        session = NewSession(agent_id=new.agent_id, title=new.title, id=new.id)
    elif command.key is not None:
        session = SessionKey(agent_id=command.key.agent_id, title=command.key.title)
    else:
        session = targets[0]
    return Inbound(
        origin=command.origin,
        origin_ref=command.origin_ref,
        content=command.content,
        session=session,
        awaited=command.awaited,
    )


def _derived_title(inbound: Inbound, attachments: Sequence[FileMeta]) -> str:
    """DerivedTitle 的新会话：文字的第一行；只发了文件时用文件名。"""
    if inbound.content.strip():
        return default_title(inbound.content)
    return default_title(", ".join(m.name for m in attachments))


async def receive(s: AsyncSession, queue: TaskQueue, inbound: Inbound) -> Received:
    """所有来信的唯一入口：确定会话（继续或新建），把它投进会话的信箱。"""
    session, attachments = await _target_in(s, inbound)
    if session is None:
        target = inbound.session
        if isinstance(target, str):
            raise RuntimeError(f"inbound for session {target!r} resolved to no session")
        if isinstance(target, SessionKey):
            spec = NewSession(agent_id=target.agent_id, title=target.title)
        elif isinstance(target.title, DerivedTitle):
            spec = replace(target, title=_derived_title(inbound, attachments))
        else:
            spec = target
        session = await create_session(s, origin=inbound.origin, origin_ref=inbound.origin_ref, spec=spec)
    session.updated_at = utcnow()
    mail = []
    if inbound.answers:
        answers = await _resolve_answers(s, session.id, inbound.answers)
        mail.append(
            await post_in(
                s,
                queue,
                session.id,
                kind=MailKind.ANSWER,
                content="",
                answers={k: UserAnswered.of(v).model_dump() for k, v in answers.items()},
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
