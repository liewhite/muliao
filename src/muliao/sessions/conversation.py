"""会话领域：子会话 thread、来信的唯一入口（投进会话信箱，sessions.mailbox）。设计见 docs/architecture.md「会话领域」。

委派都是往 thread 的信箱投信，thread 由自己的消费者处理：delegate_task 投给公开 thread（用户能看到、能插话）；
ask_agent 投给私有 thread（用户只读），发问方停在这次调用上，结局作为它的结果交回（delegations.manager）。
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.files.store import load_metas
from muliao.models import Agent, Mail, MailKind, MailState, Origin, RetiredAgent, Session, TaskStatus
from muliao.platform.db import utcnow
from muliao.platform.ids import new_id
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import TaskQueue, session_lane
from muliao.sessions.events import DelegationDispatched, Ending, SessionDeleted
from muliao.sessions.history import latest_answer, load_context
from muliao.sessions.holds import get_in as get_hold_in
from muliao.sessions.holds import has_active_in, hold_in
from muliao.sessions.mailbox import latest_run, post_in, queued_in
from muliao.sessions.pending import pending_questions
from muliao.sessions.questions import AskResult, Question, QuestionAnswer, answer_result
from muliao.sessions.settlement import failure_of, settle_in, unsettled_in

log = logging.getLogger(__name__)

THREAD_MAX = 100


def validate_thread(name: str) -> str:
    name = name.strip()
    if not name or len(name) > THREAD_MAX:
        raise InvalidInput(f"thread name must be 1-{THREAD_MAX} characters")
    return name


# ---- 入口：一切投进会话信箱的来信 ----


@dataclass(frozen=True)
class NewSession:
    """事件要求新建会话时的会话描述。"""

    agent_id: str
    title: str
    # 委派产生的 thread：父会话与 thread 名；private 为 ask_agent 开出的、用户只读的 thread
    parent_session_id: str | None = None
    thread: str | None = None
    private: bool = False


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
        if session.private:
            raise Conflict(f"session {session.id} is a private thread of another agent and is read-only")
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
    """会话只为还在的 agent 新建、继续。已退役（删除流程中，retired_agents 是会话领域自己的投影）的在本地事务里拒绝；
    agent 只会从存在变为删除，读它的行判断「存在」没有先查后写的竞态。"""
    if await s.get(RetiredAgent, agent_id) is not None:
        raise NotFound(f"agent {agent_id!r} has been deleted")
    if await s.get(Agent, agent_id) is None:
        raise NotFound(f"agent {agent_id!r} not found")


async def create_session(
    s: AsyncSession, *, origin: Origin, origin_ref: str | None, spec: NewSession
) -> Session:
    """新建会话的唯一实现（receive 与 ask_agent 占用 thread 都经过这里）。"""
    await ensure_agent_active_in(s, spec.agent_id)
    if (spec.parent_session_id is not None) != (origin == Origin.DELEGATION):
        raise ValueError(
            f"only delegation creates child sessions, got {origin} with parent {spec.parent_session_id}"
        )
    if origin == Origin.DELEGATION and spec.parent_session_id != origin_ref:
        raise ValueError(
            f"delegated session must be created by its parent {spec.parent_session_id}, not {origin_ref}"
        )
    session = Session(
        id=new_id(),
        agent_id=spec.agent_id,
        title=spec.title,
        origin=origin,
        # 委派的发起方即 parent_session_id
        origin_ref=None if origin == Origin.DELEGATION else origin_ref,
        parent_session_id=spec.parent_session_id,
        thread=spec.thread,
        private=spec.private,
    )
    s.add(session)
    await s.flush()
    return session


# ---- 回答挂起的提问 ----


@dataclass(frozen=True)
class OpenAsks:
    """还没有结果的提问分成两种：现在能回答的（answerable），与回答已提交、还在信箱里排队的（submitted）。
    回答在交给模型之前一直排着（运行失败重试时也还在），所以不需要别的条件。界面展示与投信校验都经过 open_asks。"""

    answerable: dict[str, list[Question]]
    submitted: frozenset[str]


async def open_asks(s: AsyncSession, session_id: str) -> OpenAsks:
    pending = pending_questions((await load_context(s, session_id)).messages)
    submitted = frozenset(
        k for m in await queued_in(s, session_id) if m.kind == MailKind.ANSWER for k in m.answers
    )
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


async def receive(s: AsyncSession, queue: TaskQueue, inbound: Inbound) -> Received:
    """所有来信的唯一入口：确定会话（继续或新建），把它投进会话的信箱。"""
    if not inbound.content.strip() and not inbound.attachments and not inbound.answers:
        raise InvalidInput("message must not be empty")
    await load_metas(s, inbound.attachments)
    if isinstance(inbound.session, NewSession):
        session = await create_session(
            s, origin=inbound.origin, origin_ref=inbound.origin_ref, spec=inbound.session
        )
    else:
        session = await s.get(Session, inbound.session)
        if session is None:
            raise NotFound(f"session {inbound.session!r} not found")
        _check_may_continue(session, inbound.origin, inbound.origin_ref)
        await ensure_agent_active_in(s, session.agent_id)
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


async def delete_sessions(
    s: AsyncSession, queue: TaskQueue, messaging: Messaging, roots: list[Session]
) -> None:
    """删除会话的唯一规则（删会话、删 agent 都经过这里）。

    私有 thread 只对父会话有意义，逐层随父会话删除（用户不能单独删除它，见 SessionService.delete）；用户可见的
    thread 保留为独立会话（parent 置空）。
    任一被删会话忙时拒绝（判断只看会话自己的数据），避免运行结果无处提交、工具写到被删的目录。交给被删会话、
    还没结清的委派结清为「被删除」（SessionSettled，委派领域交回发起方）。
    """
    doomed = {x.id: x for x in roots}
    frontier = list(doomed)
    while frontier:
        children = (
            await s.scalars(
                select(Session).where(Session.parent_session_id.in_(frontier), Session.private.is_(True))
            )
        ).all()
        frontier = [c.id for c in children if c.id not in doomed]
        doomed |= {c.id: c for c in children}
    for session in doomed.values():
        # 忙：lane 上有排队或运行中的任务（运行、压缩），或有人替它在外面做事（后台任务、委派出去的工作）
        if await queue.lane_busy(s, session_lane(session.id)) or await has_active_in(s, session.id):
            # 错误也会交还给模型，不带 id；定位信息记在日志里
            log.info("session deletion refused: busy", extra={"session_id": session.id})
            what = "the session" if session.id in {x.id for x in roots} else "one of its private threads"
            raise Conflict(
                f"{what} is busy (a run is queued or running, or background jobs or delegated work are still "
                "going); stop it first, then delete"
            )
    for session in doomed.values():
        # 交给被删会话、还没结清的工作交回「被删除」
        await settle_in(
            s,
            messaging,
            session,
            await unsettled_in(s, session.id, [MailState.QUEUED, MailState.CONSUMED]),
            Ending.DELETED,
            "",
        )
    for session in doomed.values():
        await s.delete(session)
        messaging.publish_in(s, SessionDeleted(session_id=session.id, agent_id=session.agent_id))
    # 立即执行删除：调用方随后可能删除 agent，数据库级联会抢先删掉这些行
    await s.flush()
    log.info("sessions deleted", extra={"session_ids": sorted(doomed)})


async def agent_busy_in(s: AsyncSession, queue: TaskQueue, agent_id: str) -> bool:
    """这个 agent 有会话忙（删除 agent 前的检查，规则同删除会话）。"""
    for session_id in (await s.scalars(select(Session.id).where(Session.agent_id == agent_id))).all():
        if await queue.lane_busy(s, session_lane(session_id)) or await has_active_in(s, session_id):
            return True
    return False


# ---- thread ----


async def threads_of(s: AsyncSession, parent_id: str) -> list[Session]:
    """父会话开出的 thread（含私有的），按创建顺序。"""
    query = select(Session).where(Session.parent_session_id == parent_id).order_by(Session.created_at)
    return list((await s.scalars(query)).all())


@dataclass(frozen=True)
class ThreadState:
    # 最近一次运行的状态；idle：还没有运行
    status: Literal["done", "failed", "running", "idle"]
    # done 时为会话最近的回答（可能为空：运行停在第一次回复之前），failed 时为错误说明，其余为空
    text: str


def delegation_holder(owner_id: str, key: str) -> str:
    """委派在发起方会话上的占用者，也是 thread 信件的关联 id 与委派行的主键（sessions.holds）。"""
    return f"delegation:{owner_id}:{key}"


class Threads:
    def __init__(self, queue: TaskQueue, messaging: Messaging):
        self._queue = queue
        self._messaging = messaging

    async def find(self, s: AsyncSession, parent_id: str, thread: str) -> Session | None:
        return await s.scalar(
            select(Session).where(
                Session.parent_session_id == parent_id, Session.thread == validate_thread(thread)
            )
        )

    async def open(
        self, s: AsyncSession, *, parent_id: str, agent: Agent, thread: str, private: bool
    ) -> Session:
        """取已有 thread 或新建。

        归属在创建时确定：thread 固定属于一个 agent；私有 thread 只能用 ask_agent 续聊，公开 thread 只能用
        delegate_task（公开 thread 由自己的消费者处理、用户也能插话，不能在调用方那一步里直接跑）。
        """
        thread = validate_thread(thread)
        existing = await self.find(s, parent_id, thread)
        if existing is not None:
            if existing.agent_id != agent.id:
                raise InvalidInput(
                    f"thread {thread!r} already belongs to another agent; use a different thread name"
                )
            if existing.private != private:
                tool = "ask_agent" if existing.private else "delegate_task"
                raise InvalidInput(f"thread {thread!r} was started with {tool}; use {tool} to continue it")
            return existing
        return await create_session(
            s,
            origin=Origin.DELEGATION,
            origin_ref=parent_id,
            spec=NewSession(
                agent_id=agent.id, title=thread, parent_session_id=parent_id, thread=thread, private=private
            ),
        )

    async def send(
        self, s: AsyncSession, *, parent_id: str, agent: Agent, thread: str, prompt: str
    ) -> Received:
        """不交回结局的委派（delegate_task）：往公开 thread 的信箱投一封用户在等的信（thread 不存在则新建）。"""
        child = await self.open(s, parent_id=parent_id, agent=agent, thread=thread, private=False)
        inbound = Inbound(
            origin=Origin.DELEGATION, origin_ref=parent_id, content=prompt, session=child.id, awaited=True
        )
        return await receive(s, self._queue, inbound)

    async def dispatch(
        self,
        s: AsyncSession,
        *,
        owner_id: str,
        agent: Agent,
        thread: str,
        prompt: str,
        private: bool,
        call_id: str | None,
        awaited: bool,
        key: str,
    ) -> Session:
        """要交回结局的委派（ask_agent、delegate_task(report_back)），在调用方的一个事务里：打开或找到 thread，在发起方
        登记占用（带 call_id 时这次调用从此算作已交出），往 thread 投一封以占用者为关联 id 的信件，发布
        DelegationDispatched（委派领域据此建委派行）。thread 不合用时抛 InvalidInput，什么都没写。按 key 幂等：
        续跑重新执行这次调用时只返回已经投递到的 thread。awaited：用户是否在等发起方的回答。"""
        holder = delegation_holder(owner_id, key)
        if await get_hold_in(s, owner_id, holder) is not None:
            existing = await s.scalar(select(Mail.session_id).where(Mail.correlation == holder).limit(1))
            if existing is None:
                raise RuntimeError(f"delegation {holder} is held but its thread mail is missing")
            child = await s.get(Session, existing)
            if child is None:
                raise RuntimeError(f"delegation {holder} points to a missing thread {existing}")
            return child
        child = await self.open(s, parent_id=owner_id, agent=agent, thread=thread, private=private)
        await hold_in(s, owner_id, holder, call_id)
        await receive(
            s,
            self._queue,
            Inbound(
                origin=Origin.DELEGATION,
                origin_ref=owner_id,
                content=prompt,
                session=child.id,
                # 没有人等 thread 的回答：结局交回发起方，由它处理
                awaited=False,
                correlation=holder,
            ),
        )
        self._messaging.publish_in(
            s,
            DelegationDispatched(
                holder=holder,
                owner_session_id=owner_id,
                child_session_id=child.id,
                call_id=call_id,
                awaited=awaited,
            ),
        )
        log.info(
            "delegation dispatched",
            extra={
                "session_id": owner_id,
                "thread_session_id": child.id,
                "holder": holder,
                "call_id": call_id,
            },
        )
        return child

    async def state(self, s: AsyncSession, child: Session) -> ThreadState:
        """thread 的状态：看最近一次运行；完成时给出会话最近的回答。"""
        run = await latest_run(s, child.id)
        if run is None:
            return ThreadState("idle", "")
        match run.status:
            case TaskStatus.DONE:
                return ThreadState("done", await latest_answer(s, child.id))
            case TaskStatus.FAILED:
                return ThreadState("failed", failure_of(run))
            case _:
                return ThreadState("running", "")
