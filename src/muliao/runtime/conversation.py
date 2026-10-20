"""会话领域：子会话 thread、来信的唯一入口（投进会话信箱，runtime.mailbox）。设计见 docs/architecture.md「会话领域」。

委派有两种：delegate_task 往公开 thread 的信箱投信（由它的消费者处理，调用方不等）；ask_agent 在发问方那一步里
直接驱动私有 thread（见 runtime.consumer.Consumer.run_inline）。
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import utcnow
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.files import load_metas
from muliao.ids import new_id
from muliao.models import (
    Agent,
    Job,
    JobStatus,
    Mail,
    MailKind,
    Message,
    Origin,
    Session,
    Task,
    TaskKind,
    TaskStatus,
)
from muliao.runtime.codec import load_messages
from muliao.runtime.mailbox import post_in, queued_in
from muliao.runtime.questions import (
    AskResult,
    Question,
    QuestionAnswer,
    answer_result,
    pending_in_session,
)
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


class ReportTo(BaseModel):
    """处理委派信件的运行结束（完成、被停止或最终失败）时把结果交给哪个会话：delegate_task(report_back) 的父会话
    （runtime.reports）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    # 发起委派时用户是否在等父会话的回答：交回的结果信件随它推送
    awaited: bool


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
    # 处理它的运行结束时把结果交回的会话；只有 delegate_task(report_back) 会带
    report_to: ReportTo | None = None


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


async def answerable_questions(s: AsyncSession, session_id: str) -> dict[str, list[Question]]:
    """这个会话现在能回答的提问：在等回答、还没有回答在信箱里排队的。界面展示与投信校验都经过这里。
    回答在交给模型之前一直排着（运行失败重试时也还在），所以不需要别的条件。"""
    pending = await pending_in_session(s, session_id)
    answered = {k for m in await queued_in(s, session_id) if m.kind == MailKind.ANSWER for k in m.answers}
    return {k: v for k, v in pending.items() if k not in answered}


async def _resolve_answers(
    s: AsyncSession, session_id: str, answers: Mapping[str, Sequence[QuestionAnswer]]
) -> dict[str, AskResult]:
    """用户提交的回答：每个都对应还在等回答的提问。返回交给模型的结果。"""
    pending = await answerable_questions(s, session_id)
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
                report_to=None if inbound.report_to is None else inbound.report_to.model_dump(),
            )
        )
    log.info(
        "message received",
        extra={"session_id": session.id, "origin": inbound.origin, "origin_ref": inbound.origin_ref},
    )
    return Received(session, mail)


async def latest_answer(s: AsyncSession, session_id: str) -> str:
    """会话最近的回答：最后一条模型回复里的文本；还没有回复时为空（运行停在第一次回复之前、或没有可交给模型的）。"""
    payloads = await s.scalars(
        select(Message.payload).where(Message.session_id == session_id).order_by(Message.id.desc())
    )
    for payload in payloads:
        (message,) = load_messages([payload])
        if isinstance(message, ModelResponse):
            return "\n".join(p.content for p in message.parts if isinstance(p, TextPart))
    return ""


def failure_of(turn: Task) -> str:
    """失败的运行的错误说明；队列契约保证 failed 一定带 error。"""
    if turn.status != TaskStatus.FAILED or turn.error is None:
        raise RuntimeError(
            f"turn {turn.id} is {turn.status} with error {turn.error!r}, expected a recorded failure"
        )
    return turn.error


async def delete_sessions(s: AsyncSession, roots: list[Session]) -> None:
    """删除会话的唯一规则（删会话、删 agent 都经过这里）。

    私有 thread 只对父会话有意义，逐层随之删除；用户可见的 thread 保留为独立会话（parent 置空）。
    任一被删会话正在执行时拒绝，避免运行结果无处提交、工具写到被删的目录。私有 thread 的内联运行同样记为
    running（TaskQueue.start_inline），所以只看各会话自己的运行中 task。
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


def thread_name(session: Session) -> str:
    """thread 会话的名字；委派建会话时一定写入（迁移回填时为会话 id）。"""
    if session.thread is None:
        raise RuntimeError(f"session {session.id} has a parent but no thread name")
    return session.thread


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
        report_to: ReportTo | None,
    ) -> Received:
        """往公开 thread 的信箱投信（thread 不存在则新建），调用方不等。返回投递到的会话与投进的信件。

        回答推送给用户；要交回调用方时（report_to）不推送，结果作为事件交给调用方的会话，由它决定怎么告诉用户。"""
        child = await self.open(s, parent_id=parent_id, agent=agent, thread=thread, private=False)
        inbound = Inbound(
            origin=Origin.DELEGATION,
            origin_ref=parent_id,
            content=prompt,
            session=child.id,
            awaited=report_to is None,
            report_to=report_to,
        )
        return await receive(s, self._queue, inbound)

    async def state(self, s: AsyncSession, child: Session) -> ThreadState:
        """thread 的状态：看最近一次运行；完成时给出会话最近的回答。"""
        turn = await s.scalar(
            select(Task)
            .where(Task.session_id == child.id, Task.kind == TaskKind.TURN)
            .order_by(Task.id.desc())
            .limit(1)
        )
        if turn is None:
            return ThreadState("idle", "")
        match turn.status:
            case TaskStatus.DONE:
                return ThreadState("done", await latest_answer(s, child.id))
            case TaskStatus.FAILED:
                return ThreadState("failed", failure_of(turn))
            case _:
                return ThreadState("running", "")
