"""会话领域：子会话 thread、来信的唯一入口（投进会话信箱，runtime.mailbox）。设计见 docs/architecture.md「会话领域」。

委派都是往 thread 的信箱投信，thread 由自己的消费者处理：delegate_task 投给公开 thread（用户能看到、能插话）；
ask_agent 投给私有 thread（用户只读），发问方停在这次调用上，结局作为它的结果交回（runtime.delegations）。
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import utcnow
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.files import load_metas
from muliao.ids import new_id
from muliao.models import (
    Agent,
    Delegation,
    DelegationStatus,
    Job,
    JobStatus,
    Mail,
    MailKind,
    Origin,
    Session,
    Task,
    TaskStatus,
)
from muliao.runtime.delegations import Ending, Outcome, settle_all_in, start_in
from muliao.runtime.history import latest_answer, load_context
from muliao.runtime.mailbox import latest_run, post_in, queued_in
from muliao.runtime.pending import pending_questions
from muliao.runtime.questions import AskResult, Question, QuestionAnswer, answer_result
from muliao.runtime.tasks import TaskQueue, running_task_ids

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
    """一封来信：用户消息、cron 到点、外部事件、agent 委派，都是「把 content 投进某个会话的信箱」（runtime.mailbox）。

    session 为已有会话的 id 时在其中继续，为 NewSession 时新建会话。
    origin_ref 标识具体的发送方：触发器 id、委派方（父）会话 id；用户为 None。
    """

    origin: Origin
    origin_ref: str | None
    content: str
    session: str | NewSession
    # 用户是否在等它的回答（处理它的运行结束时推送）
    awaited: bool
    # 随消息发送的文件 id（muliao.files）；只有用户在界面上发的消息会带
    attachments: tuple[str, ...] = ()
    # 对这个会话里挂起提问的回答：tool_call_id -> 每题的回答（runtime.questions）；只有用户会带
    answers: Mapping[str, Sequence[QuestionAnswer]] = field(default_factory=dict)
    # 要交回结局的委派（runtime.delegations）；只有 Threads.send 会带
    delegation_id: int | None = None


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


async def create_session(
    s: AsyncSession, *, origin: Origin, origin_ref: str | None, spec: NewSession
) -> Session:
    """新建会话的唯一实现（receive 与 ask_agent 占用 thread 都经过这里）。"""
    if await s.get(Agent, spec.agent_id) is None:
        raise NotFound(f"agent {spec.agent_id!r} not found")
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
                delegation_id=inbound.delegation_id,
            )
        )
    log.info(
        "message received",
        extra={"session_id": session.id, "origin": inbound.origin, "origin_ref": inbound.origin_ref},
    )
    return Received(session, mail)


def failure_of(run: Task) -> str:
    """失败的运行的错误说明；队列契约保证 failed 一定带 error。"""
    if run.status != TaskStatus.FAILED or run.error is None:
        raise RuntimeError(
            f"run {run.id} is {run.status} with error {run.error!r}, expected a recorded failure"
        )
    return run.error


async def delete_sessions(s: AsyncSession, queue: TaskQueue, roots: list[Session]) -> None:
    """删除会话的唯一规则（删会话、删 agent 都经过这里）。

    私有 thread 只对父会话有意义，逐层随父会话删除（用户不能单独删除它，见 SessionService.delete）；用户可见的
    thread 保留为独立会话（parent 置空）。
    任一被删会话正在执行时拒绝，避免运行结果无处提交、工具写到被删的目录。还在等被删 thread 交回结局的委派
    交回「被删除」（发起会话本身也被删除的除外：委派随它删除）。
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
        running = await running_task_ids(s, session_id=session.id)
        if running:
            # 错误也会交还给模型，不带 id；定位信息记在日志里
            log.info(
                "session deletion refused: tasks running", extra={"session_id": session.id, "tasks": running}
            )
            what = "the session" if session.id in {x.id for x in roots} else "one of its private threads"
            raise Conflict(f"{what} has a task running right now; wait for it to finish, then delete")
    # 后台任务的进程不随会话删除结束，结束时也无处投递：先由 agent 或用户结束它们
    if running_jobs := (
        await s.scalars(
            select(Job.id).where(Job.session_id.in_(list(doomed)), Job.status == JobStatus.RUNNING)
        )
    ).all():
        log.info("session deletion refused: background jobs running", extra={"jobs": list(running_jobs)})
        raise Conflict(
            f"the session has background jobs running ({', '.join(map(str, running_jobs))}); "
            "kill them (job_kill) or wait for them to finish, then delete"
        )
    pending = await s.scalars(
        select(Delegation).where(
            Delegation.child_session_id.in_(list(doomed)),
            Delegation.owner_session_id.not_in(list(doomed)),
            Delegation.status == DelegationStatus.RUNNING,
        )
    )
    for delegation in pending.all():
        await settle_all_in(
            s, queue, [delegation], doomed[delegation.child_session_id], Outcome(Ending.DELETED, "")
        )
    for session in doomed.values():
        await s.delete(session)
    # 立即执行删除：调用方随后可能删除 agent，数据库级联会抢先删掉这些行
    await s.flush()
    log.info("sessions deleted", extra={"session_ids": sorted(doomed)})


# ---- thread ----


async def threads_of(s: AsyncSession, parent_id: str) -> list[Session]:
    """父会话开出的 thread（含私有的），按创建顺序。"""
    query = select(Session).where(Session.parent_session_id == parent_id).order_by(Session.created_at)
    return list((await s.scalars(query)).all())


@dataclass(frozen=True)
class ReportBack:
    """委派要交回结局：call_id 为 ask_agent 的那次调用（结局作为它的结果），report_back 时为 None（作为事件）；
    awaited 为用户是否在等发起方的回答。"""

    call_id: str | None
    awaited: bool


@dataclass(frozen=True)
class ThreadState:
    # 最近一次运行的状态；idle：还没有运行
    status: Literal["done", "failed", "running", "idle"]
    # done 时为会话最近的回答（可能为空：运行停在第一次回复之前），failed 时为错误说明，其余为空
    text: str


class Threads:
    def __init__(self, queue: TaskQueue):
        self._queue = queue

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
        self,
        s: AsyncSession,
        *,
        parent_id: str,
        agent: Agent,
        thread: str,
        prompt: str,
        private: bool,
        report: ReportBack | None,
    ) -> Received:
        """往 thread 的信箱投信（thread 不存在则新建），返回投递到的会话与投进的信件。

        要交回结局时（report）建一个委派，信件带着它，没有人等 thread 的回答（发起方处理结局）；否则用户在等它
        （delegate_task 的回答推送给用户）。"""
        child = await self.open(s, parent_id=parent_id, agent=agent, thread=thread, private=private)
        delegation = (
            None
            if report is None
            else await start_in(
                s, owner_id=parent_id, child_id=child.id, call_id=report.call_id, awaited=report.awaited
            )
        )
        inbound = Inbound(
            origin=Origin.DELEGATION,
            origin_ref=parent_id,
            content=prompt,
            session=child.id,
            awaited=report is None,
            delegation_id=None if delegation is None else delegation.id,
        )
        return await receive(s, self._queue, inbound)

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
