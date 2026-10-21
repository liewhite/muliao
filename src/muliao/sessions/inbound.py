"""来信：一切投进会话信箱的东西的唯一入口（docs/architecture.md 第 14 节「触发源与通知」）。

- receive：用户消息、对提问的回答、委派来的任务，在调用方的事务里；check_receivable_in 是同一套校验规则（只读），
  触发器先在自己的事务里校验，再发出定向命令 Receive（receive_command / inbound_of）。
- 续写规则：用户可以在任何用户能写的会话里继续；其他来源只能续写自己发起的会话。
- 新建会话只有 create_session 一处实现（来信与 thread 都经过它）。"""

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import active_agent_in
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.files.api import load_metas
from muliao.platform.db import utcnow
from muliao.platform.ids import new_id
from muliao.platform.tasks import TaskQueue
from muliao.platform.tooling import Origin
from muliao.sessions.events import Receive, ReceiveNewSession
from muliao.sessions.history import since_last_response, since_last_response_among
from muliao.sessions.kinds import SessionKind, policy_of
from muliao.sessions.mailbox import post_in, queued_among, queued_in
from muliao.sessions.pending import UserAnswered, pending_questions
from muliao.sessions.questions import AskResult, Question, QuestionAnswer, answer_result
from muliao.sessions.tables import Mail, MailKind, Session

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewSession:
    """事件要求新建会话时的会话描述。"""

    agent_id: str
    title: str
    # 委派产生的 thread：父会话、thread 名与种类
    parent_session_id: str | None = None
    thread: str | None = None
    kind: SessionKind = SessionKind.CONVERSATION
    # 发送方预先分配的会话 id（经定向命令异步投递时，发送方要立即告诉它的调用方会话 id，见 Receive）；None 时生成
    id: str | None = None


@dataclass(frozen=True)
class Inbound:
    """一封来信：用户消息、cron 到点、外部事件、agent 委派，都是「把 content 投进某个会话的信箱」（sessions.mailbox）。

    session 为已有会话的 id 时在其中继续，为 NewSession 时新建会话。
    origin_ref 标识具体的发送方：触发器 id、委派方（父）会话 id；用户为 None。
    """

    origin: Origin
    origin_ref: str | None
    content: str
    session: str | NewSession
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
    await ensure_agent_active_in(s, spec.agent_id)
    is_thread = origin == Origin.DELEGATION
    if (
        (spec.parent_session_id is not None) != is_thread
        or (spec.kind != SessionKind.CONVERSATION) != is_thread
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


async def _existing_target_in(s: AsyncSession, inbound: Inbound) -> Session | None:
    """来信的校验规则（只读）：内容不为空、附件存在、目标会话存在且发送方可以续写、agent 还可用。返回要续写的会话，
    新建时为 None。"""
    if not inbound.content.strip() and not inbound.attachments and not inbound.answers:
        raise InvalidInput("message must not be empty")
    await load_metas(s, inbound.attachments)
    if isinstance(inbound.session, NewSession):
        await ensure_agent_active_in(s, inbound.session.agent_id)
        return None
    session = await s.get(Session, inbound.session)
    if session is None:
        raise NotFound(f"session {inbound.session!r} not found")
    _check_may_continue(session, inbound.origin, inbound.origin_ref)
    await ensure_agent_active_in(s, session.agent_id)
    return session


async def check_receivable_in(s: AsyncSession, inbound: Inbound) -> None:
    """在发送方的事务里校验一封来信（不写入），之后经定向命令 Receive 投递。校验读的都是只会单向变化或不变的事实
    （会话与 agent 是否还在、会话的发起方、文件），投递时只剩「期间被删除」这一种契约内的变化（docs/architecture.md
    2.2）。"""
    await _existing_target_in(s, inbound)


def receive_target(command: Receive) -> str:
    if command.new_session is not None:
        return command.new_session.id
    if command.session_id is None:
        raise ValueError(f"Receive needs a session id or a new session, got {command}")
    return command.session_id


def receive_command(inbound: Inbound) -> Receive:
    """一封要经定向命令投递的来信（发送方先 check_receivable_in）。新建会话必须带预先分配的 id；只有文字内容的来信
    才能这样投（附件、回答、关联只来自同步的入口）。"""
    if inbound.attachments or inbound.answers or inbound.correlation is not None:
        raise ValueError("only plain text inbound mail can be sent as a Receive command")
    if isinstance(inbound.session, str):
        return Receive(
            origin=inbound.origin,
            origin_ref=inbound.origin_ref,
            content=inbound.content,
            awaited=inbound.awaited,
            session_id=inbound.session,
            new_session=None,
        )
    spec = inbound.session
    if spec.id is None or spec.kind != SessionKind.CONVERSATION:
        raise ValueError(f"a Receive command creates a top-level session with a preassigned id, got {spec}")
    return Receive(
        origin=inbound.origin,
        origin_ref=inbound.origin_ref,
        content=inbound.content,
        awaited=inbound.awaited,
        session_id=None,
        new_session=ReceiveNewSession(id=spec.id, agent_id=spec.agent_id, title=spec.title),
    )


def inbound_of(command: Receive) -> Inbound:
    if (command.session_id is None) == (command.new_session is None):
        raise ValueError(f"Receive needs exactly one of session_id and new_session, got {command}")
    new = command.new_session
    return Inbound(
        origin=command.origin,
        origin_ref=command.origin_ref,
        content=command.content,
        session=command.session_id
        if new is None
        else NewSession(agent_id=new.agent_id, title=new.title, id=new.id),
        awaited=command.awaited,
    )


async def receive(s: AsyncSession, queue: TaskQueue, inbound: Inbound) -> Received:
    """所有来信的唯一入口：确定会话（继续或新建），把它投进会话的信箱。"""
    session = await _existing_target_in(s, inbound)
    if session is None:
        if not isinstance(inbound.session, NewSession):
            raise RuntimeError(f"inbound for session {inbound.session!r} resolved to no session")
        session = await create_session(
            s, origin=inbound.origin, origin_ref=inbound.origin_ref, spec=inbound.session
        )
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
