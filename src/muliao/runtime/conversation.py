"""会话领域：子会话 thread、按轮的结果路由、一轮对话的持久化挂起与唤醒。设计见 docs/architecture.md「会话领域」。

一轮对话就是一个 turn task，它的状态就是这一轮的状态（单一事实源）：
- 以待完成的工具调用（ask_agent / wait_for）结束时，task 挂起为 waiting，并写下等待记录（waits）；
- 所等的轮次全部结束后转回 pending，再次执行时带着结果接着跑；
- 直到给出最终回答（done），或彻底失败（failed，整轮回滚）。
"""

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import utcnow
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.ids import new_id
from muliao.models import Agent, Message, Origin, ReplyTo, Session, Task, TaskKind, TaskStatus, Wait
from muliao.runtime.codec import load_messages
from muliao.runtime.tasks import TaskQueue, running_task_ids, turn_lane

log = logging.getLogger(__name__)

THREAD_MAX = 100


def validate_thread(name: str) -> str:
    name = name.strip()
    if not name or len(name) > THREAD_MAX:
        raise InvalidInput(f"thread name must be 1-{THREAD_MAX} characters")
    return name


# ---- 入口：一切「交给 agent 处理一轮」的事件 ----


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
    """一个事件：用户消息、cron 到点、外部事件、agent 委派，都是「把 content 交给某个 agent 处理一轮」。

    session 为已有会话的 id 时在其中继续，为 NewSession 时新建会话。
    source_ref 标识具体的发送方：cron job id、事件触发器 id、委派方（父）会话 id；用户为 None。
    """

    source: Origin
    source_ref: str | None
    content: str
    session: str | NewSession
    reply_to: ReplyTo


@dataclass(frozen=True)
class Received:
    session: Session
    turn: Task


def _sender_of(session: Session) -> tuple[Origin, str | None]:
    """会话的发起方。委派会话的发起方就是它的父会话（存在 parent_session_id，不另存副本）。"""
    if session.origin == Origin.DELEGATION:
        return session.origin, session.parent_session_id
    return session.origin, session.origin_ref


def _check_may_continue(session: Session, source: Origin, source_ref: str | None) -> None:
    """续写规则：用户可以在任何非私有会话里继续；其他来源只能续写自己发起的会话。"""
    if source == Origin.USER:
        if session.private:
            raise Conflict(f"session {session.id} is a private thread of another agent and is read-only")
        return
    if _sender_of(session) != (source, source_ref):
        log.warning(
            "continuation refused",
            extra={"session_id": session.id, "source": source, "source_ref": source_ref},
        )
        raise Forbidden(
            f"session {session.id} was not started by this sender; it may only continue its own sessions"
        )


async def create_session(
    s: AsyncSession, *, source: Origin, source_ref: str | None, spec: NewSession
) -> Session:
    """新建会话的唯一实现（receive 与 ask_agent 占用 thread 都经过这里）。"""
    if await s.get(Agent, spec.agent_id) is None:
        raise NotFound(f"agent {spec.agent_id!r} not found")
    if (spec.parent_session_id is not None) != (source == Origin.DELEGATION):
        raise ValueError(
            f"only delegation creates child sessions, got {source} with parent {spec.parent_session_id}"
        )
    if source == Origin.DELEGATION and spec.parent_session_id != source_ref:
        raise ValueError(
            f"delegated session must be created by its parent {spec.parent_session_id}, not {source_ref}"
        )
    session = Session(
        id=new_id(),
        agent_id=spec.agent_id,
        title=spec.title,
        origin=source,
        # 委派的发起方即 parent_session_id
        origin_ref=None if source == Origin.DELEGATION else source_ref,
        parent_session_id=spec.parent_session_id,
        thread=spec.thread,
        private=spec.private,
    )
    s.add(session)
    await s.flush()
    return session


async def receive(s: AsyncSession, queue: TaskQueue, inbound: Inbound) -> Received:
    """所有新一轮对话的唯一入口：确定会话（继续或新建），投递一轮。"""
    if not inbound.content.strip():
        raise InvalidInput("message must not be empty")
    if isinstance(inbound.session, NewSession):
        session = await create_session(
            s, source=inbound.source, source_ref=inbound.source_ref, spec=inbound.session
        )
    else:
        session = await s.get(Session, inbound.session)
        if session is None:
            raise NotFound(f"session {inbound.session!r} not found")
        _check_may_continue(session, inbound.source, inbound.source_ref)
    session.updated_at = utcnow()
    turn = queue.enqueue(
        s,
        kind=TaskKind.TURN,
        lane=turn_lane(session.id),
        agent_id=session.agent_id,
        session_id=session.id,
        payload={"prompt": inbound.content, "reply_to": inbound.reply_to},
    )
    log.info(
        "message received",
        extra={"session_id": session.id, "source": inbound.source, "source_ref": inbound.source_ref},
    )
    return Received(session, turn)


async def answer_of(s: AsyncSession, turn_id: int) -> str:
    """已完成的一轮的最终回答：该轮最后一条模型响应里的文本。"""
    payloads = (
        await s.scalars(select(Message.payload).where(Message.task_id == turn_id).order_by(Message.id))
    ).all()
    for message in reversed(load_messages(payloads)):
        if isinstance(message, ModelResponse):
            return "\n".join(p.content for p in message.parts if isinstance(p, TextPart))
    raise RuntimeError(f"turn {turn_id} finished without a model response")


def failure_of(turn: Task) -> str:
    """失败的一轮的错误说明；队列契约保证 failed 一定带 error。"""
    if turn.status != TaskStatus.FAILED or turn.error is None:
        raise RuntimeError(
            f"turn {turn.id} is {turn.status} with error {turn.error!r}, expected a recorded failure"
        )
    return turn.error


async def _outcome_of(s: AsyncSession, turn_id: int) -> tuple[str | None, str | None] | None:
    """所等的一轮若已结束（或已被删除），返回 (result, error)；仍在进行返回 None。"""
    target = await s.get(Task, turn_id)
    if target is None:
        return None, "the awaited thread was deleted"
    match target.status:
        case TaskStatus.DONE:
            return await answer_of(s, turn_id), None
        case TaskStatus.FAILED:
            return None, failure_of(target)
        case _:
            return None


# ---- 等待关系 ----


async def resolve_waits(
    s: AsyncSession, queue: TaskQueue, turn_id: int, *, result: str | None = None, error: str | None = None
) -> None:
    """一轮结束：把结果（或错误）写进所有还在等它的记录，结果齐了的等待方转回 pending。

    已有结果的记录不再改写：目标失败后被人工重试时，等待方早已带着错误恢复运行。
    """
    if (result is None) == (error is None):
        raise ValueError("exactly one of result or error must be given")
    waits = (
        await s.scalars(
            select(Wait).where(Wait.target_task_id == turn_id, Wait.result.is_(None), Wait.error.is_(None))
        )
    ).all()
    for w in waits:
        w.result, w.error = result, error
    await s.flush()
    for waiter_id in {w.waiter_task_id for w in waits}:
        if not await _has_open_waits(s, waiter_id):
            waiter = await s.get(Task, waiter_id)
            if waiter is None:
                raise RuntimeError(f"wait references missing waiter task {waiter_id}")
            queue.release(s, waiter)


async def _has_open_waits(s: AsyncSession, waiter_id: int) -> bool:
    open_wait = await s.scalar(
        select(Wait.id)
        .where(Wait.waiter_task_id == waiter_id, Wait.result.is_(None), Wait.error.is_(None))
        .limit(1)
    )
    return open_wait is not None


async def collected(s: AsyncSession, turn: Task) -> dict[str, str]:
    """挂起的一轮恢复时交给模型的工具结果：tool_call_id → 文本（失败时为错误说明）。

    没有等待记录说明这一轮还没挂起过（回滚会清掉等待记录），返回空表，由调用方从 prompt 开始。
    """
    results: dict[str, str] = {}
    for w in (await s.scalars(select(Wait).where(Wait.waiter_task_id == turn.id))).all():
        if w.result is not None:
            results[w.tool_call_id] = w.result
        elif w.error is not None:
            results[w.tool_call_id] = f"[failed] {w.error}"
        else:
            raise RuntimeError(f"turn {turn.id} resumed before wait {w.id} was resolved")
    return results


async def consume_waits(s: AsyncSession, turn: Task) -> None:
    """恢复运行的结果已随消息一起提交：上一次挂起的等待记录完成使命。"""
    await s.execute(delete(Wait).where(Wait.waiter_task_id == turn.id))


async def roll_back_turn(s: AsyncSession, queue: TaskQueue, turn: Task) -> None:
    """一轮彻底失败：撤销它已提交的消息和等待记录，让等它的 agent 收到错误。

    一轮是原子的。挂起过的一轮已提交了带工具调用的消息，留着它们，之后每一轮的历史都会被模型 API 拒绝；
    回滚后人工重试就是从 prompt 重跑整轮。
    """
    removed = (await s.execute(delete(Message).where(Message.task_id == turn.id))).rowcount
    await consume_waits(s, turn)
    if removed:
        log.warning(
            "failed turn rolled back",
            extra={"session_id": turn.session_id, "task_id": turn.id, "messages_removed": removed},
        )
    await resolve_waits(s, queue, turn.id, error=failure_of(turn))


async def delete_sessions(s: AsyncSession, queue: TaskQueue, roots: list[Session]) -> None:
    """删除会话的唯一规则（删会话、删 agent 都经过这里）。

    私有 thread 只对父会话有意义，逐层随之删除；用户可见的 thread 保留为独立会话（parent 置空）。
    任一被删会话有 task 正在运行时拒绝，避免运行结果无处提交；等待被删轮次的 agent 收到错误，而不是永远等下去。
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
        if running := await running_task_ids(s, session_id=session.id):
            # 错误也会交还给模型，不带 id；定位信息记在日志里
            log.info(
                "session deletion refused: tasks running", extra={"session_id": session.id, "tasks": running}
            )
            what = "the session" if session.id in {x.id for x in roots} else "one of its private threads"
            raise Conflict(f"{what} has a task running right now; wait for it to finish, then delete")
    for session in doomed.values():
        reason = f"session {session.id} was deleted"
        for turn_id in (await s.scalars(select(Task.id).where(Task.session_id == session.id))).all():
            await resolve_waits(s, queue, turn_id, error=reason)
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
    # idle：thread 已被占用但还没有任何一轮（占用它的那一轮回滚了）
    status: Literal["done", "failed", "running", "idle"]
    # done 时为最终回答，failed 时为错误说明，其余为空
    text: str
    # running 时为要等的那一轮
    turn_id: int | None


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

        归属在创建时确定：thread 固定属于一个 agent；私有 thread 不能被 delegate_task 变成用户可见。
        """
        thread = validate_thread(thread)
        existing = await self.find(s, parent_id, thread)
        if existing is not None:
            if existing.agent_id != agent.id:
                raise InvalidInput(
                    f"thread {thread!r} already belongs to another agent; use a different thread name"
                )
            if existing.private and not private:
                raise InvalidInput(
                    f"thread {thread!r} is private to this conversation (created by ask_agent); "
                    "use ask_agent to continue it"
                )
            return existing
        return await create_session(
            s,
            source=Origin.DELEGATION,
            source_ref=parent_id,
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
        reply_to: ReplyTo,
    ) -> Task:
        """在 thread 里发起新的一轮（thread 不存在则新建）。"""
        child = await self.open(s, parent_id=parent_id, agent=agent, thread=thread, private=private)
        inbound = Inbound(
            source=Origin.DELEGATION,
            source_ref=parent_id,
            content=prompt,
            session=child.id,
            reply_to=reply_to,
        )
        return (await receive(s, self._queue, inbound)).turn

    async def state(self, s: AsyncSession, child: Session) -> ThreadState:
        """thread 最近一轮的状态。"""
        turn = await s.scalar(
            select(Task)
            .where(Task.session_id == child.id, Task.kind == TaskKind.TURN)
            .order_by(Task.id.desc())
            .limit(1)
        )
        if turn is None:
            return ThreadState("idle", "", None)
        match turn.status:
            case TaskStatus.DONE:
                return ThreadState("done", await answer_of(s, turn.id), None)
            case TaskStatus.FAILED:
                return ThreadState("failed", failure_of(turn), None)
            case _:
                return ThreadState("running", "", turn.id)

    async def suspend(
        self, s: AsyncSession, turn: Task, session: Session, deferred: DeferredToolRequests
    ) -> TaskStatus:
        """这一轮以待完成的工具调用结束：写等待记录，并投递 ask 的子任务。

        返回这一轮接下来的状态：通常是 waiting；若所等的轮次都已结束（工具判断「仍在运行」之后、
        本次提交之前结束或被删除），结果就地写入，返回 pending 直接接着跑。
        """
        for call in deferred.calls:
            meta = deferred.metadata.get(call.tool_call_id)
            if meta is None:
                raise RuntimeError(
                    f"deferred call {call.tool_name} ({call.tool_call_id}) carries no metadata"
                )
            match meta["action"]:
                case "ask":
                    agent = await s.get(Agent, meta["agent_id"])
                    if agent is None:
                        raise NotFound(f"agent {meta['agent_id']!r} not found")
                    target = await self.send(
                        s,
                        parent_id=session.id,
                        agent=agent,
                        thread=meta["thread"],
                        prompt=meta["message"],
                        private=True,
                        reply_to=ReplyTo.AGENT,
                    )
                    await s.flush()
                    s.add(
                        Wait(waiter_task_id=turn.id, tool_call_id=call.tool_call_id, target_task_id=target.id)
                    )
                case "wait":
                    turn_id = int(meta["turn_id"])
                    outcome = await _outcome_of(s, turn_id)
                    result, error = outcome if outcome is not None else (None, None)
                    s.add(
                        Wait(
                            waiter_task_id=turn.id,
                            tool_call_id=call.tool_call_id,
                            target_task_id=turn_id,
                            result=result,
                            error=error,
                        )
                    )
                case other:
                    raise RuntimeError(f"unknown deferred action {other!r} on {call.tool_name}")
        await s.flush()
        status = TaskStatus.WAITING if await _has_open_waits(s, turn.id) else TaskStatus.PENDING
        log.info(
            "turn suspended",
            extra={
                "session_id": session.id,
                "task_id": turn.id,
                "calls": len(deferred.calls),
                "next": status,
            },
        )
        return status
