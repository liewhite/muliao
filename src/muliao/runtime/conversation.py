"""会话领域：子会话 thread、按轮的结果路由、父 agent 的持久化挂起与唤醒。设计见 docs/architecture.md「会话领域」。

一轮对话可能拆成多个任务：turn → 挂起 → resume（→ 再挂起 → resume …）。这一串任务称为一轮，
以最初的 turn 任务 id（root）标识：消息都记在 root 名下，等待关系随挂起从旧任务转移到新的 resume 任务。
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import utcnow
from muliao.errors import InvalidInput, NotFound
from muliao.ids import new_id
from muliao.models import Agent, Message, Origin, ReplyTo, Session, Task, TaskKind, TaskStatus, Wait
from muliao.runtime.codec import load_messages
from muliao.runtime.tasks import TaskQueue, turn_lane

log = logging.getLogger(__name__)

THREAD_MAX = 100
_TERMINAL = (TaskStatus.DONE, TaskStatus.FAILED)


def root_of(task: Task) -> int:
    """task 所属那一轮的 root turn id。"""
    match task.kind:
        case TaskKind.TURN:
            return task.id
        case TaskKind.RESUME:
            return int(task.payload["turn_task_id"])
        case _:
            raise ValueError(f"task {task.id} of kind {task.kind} is not part of a conversation turn")


def validate_thread(name: str) -> str:
    name = name.strip()
    if not name or len(name) > THREAD_MAX:
        raise InvalidInput(f"thread name must be 1-{THREAD_MAX} characters")
    return name


async def answer_of(s: AsyncSession, root_task_id: int) -> str:
    """一轮的最终回答：该轮最后一条模型响应里的文本。"""
    payloads = (
        await s.scalars(select(Message.payload).where(Message.task_id == root_task_id).order_by(Message.id))
    ).all()
    for message in reversed(load_messages(payloads)):
        if isinstance(message, ModelResponse):
            return "\n".join(p.content for p in message.parts if isinstance(p, TextPart))
    raise RuntimeError(f"turn {root_task_id} finished without a model response")


def enqueue_turn(s: AsyncSession, queue: TaskQueue, session: Session, prompt: str, reply_to: ReplyTo) -> Task:
    """所有新一轮对话的唯一入口：用户、cron、委派都经过这里。"""
    if not prompt.strip():
        raise InvalidInput("prompt must not be empty")
    session.updated_at = utcnow()
    return queue.enqueue(
        s,
        kind=TaskKind.TURN,
        lane=turn_lane(session.id),
        agent_id=session.agent_id,
        session_id=session.id,
        payload={"prompt": prompt, "reply_to": reply_to},
    )


# ---- 等待关系 ----


async def _release_if_ready(s: AsyncSession, queue: TaskQueue, waiter_ids: set[int]) -> None:
    for waiter_id in waiter_ids:
        waits = (await s.scalars(select(Wait).where(Wait.waiter_task_id == waiter_id))).all()
        if all(w.result is not None or w.error is not None for w in waits):
            waiter = await s.get(Task, waiter_id)
            if waiter is None:
                raise RuntimeError(f"wait references missing waiter task {waiter_id}")
            queue.release(s, waiter)


async def resolve_waits(
    s: AsyncSession,
    queue: TaskQueue,
    target_task_id: int,
    *,
    result: str | None = None,
    error: str | None = None,
) -> None:
    """目标任务结束：把结果（或错误）写进所有还在等它的记录，结果齐了的等待方转为 pending。

    已有结果的记录不再改写：目标失败后被人工重试成功时，等待方早已带着错误恢复运行。
    """
    if (result is None) == (error is None):
        raise ValueError("exactly one of result or error must be given")
    waits = (
        await s.scalars(
            select(Wait).where(
                Wait.target_task_id == target_task_id, Wait.result.is_(None), Wait.error.is_(None)
            )
        )
    ).all()
    for w in waits:
        w.result, w.error = result, error
    await s.flush()
    await _release_if_ready(s, queue, {w.waiter_task_id for w in waits})


async def retarget_waits(s: AsyncSession, old_task_id: int, new_task_id: int) -> None:
    """目标任务自己挂起：等它的人改为等它的 resume 任务。"""
    for w in (await s.scalars(select(Wait).where(Wait.target_task_id == old_task_id))).all():
        w.target_task_id = new_task_id


async def fail_waits_on_session(s: AsyncSession, queue: TaskQueue, session_id: str, reason: str) -> None:
    """session 即将被删除：等它任何任务的父 agent 都收到错误，而不是永远等下去。"""
    task_ids = (await s.scalars(select(Task.id).where(Task.session_id == session_id))).all()
    for task_id in task_ids:
        await resolve_waits(s, queue, task_id, error=reason)


# ---- thread ----


@dataclass(frozen=True)
class ThreadState:
    status: Literal["done", "failed", "running"]
    # done 时为最终回答，failed 时为错误说明，running 时为空
    text: str
    # running 时为当前要等的任务（这一轮链条上尚未结束的那个）
    target_task_id: int | None


class Threads:
    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def find(self, s: AsyncSession, parent_id: str, thread: str) -> Session | None:
        return await s.scalar(
            select(Session).where(
                Session.parent_session_id == parent_id, Session.thread == validate_thread(thread)
            )
        )

    async def check(
        self, s: AsyncSession, *, parent_id: str, agent: Agent, thread: str, private: bool
    ) -> Session | None:
        """检查能否以给定方式使用 thread，返回已有的 thread（没有则为 None）。

        归属在创建时确定：thread 固定属于一个 agent；私有 thread 不能被 delegate_task 变成用户可见。
        """
        existing = await self.find(s, parent_id, thread)
        if existing is None:
            return None
        if existing.agent_id != agent.id:
            raise InvalidInput(
                f"thread {thread!r} already belongs to another agent; use a different thread name"
            )
        if existing.private and not private:
            raise InvalidInput(
                f"thread {thread!r} is private to this conversation (created by ask_agent); use ask_agent to continue it"
            )
        return existing

    async def open(
        self, s: AsyncSession, *, parent: Session, agent: Agent, thread: str, private: bool
    ) -> Session:
        """取已有 thread 或新建。"""
        thread = validate_thread(thread)
        existing = await self.check(s, parent_id=parent.id, agent=agent, thread=thread, private=private)
        if existing is not None:
            return existing
        session = Session(
            id=new_id(),
            agent_id=agent.id,
            title=thread,
            origin=Origin.DELEGATION,
            origin_ref=None,
            parent_session_id=parent.id,
            thread=thread,
            private=private,
        )
        s.add(session)
        await s.flush()
        return session

    async def send(
        self,
        s: AsyncSession,
        *,
        parent: Session,
        agent: Agent,
        thread: str,
        prompt: str,
        private: bool,
        reply_to: ReplyTo,
    ) -> Task:
        """在 thread 里发起新的一轮（thread 不存在则新建）。"""
        child = await self.open(s, parent=parent, agent=agent, thread=thread, private=private)
        return enqueue_turn(s, self._queue, child, prompt, reply_to)

    async def state(self, s: AsyncSession, child: Session) -> ThreadState:
        """thread 最近一轮的状态。"""
        root = await s.scalar(
            select(Task)
            .where(Task.session_id == child.id, Task.kind == TaskKind.TURN)
            .order_by(Task.id.desc())
            .limit(1)
        )
        if root is None:
            raise RuntimeError(f"thread session {child.id} has no turn")
        chain = [root] + list(
            (
                await s.scalars(
                    select(Task)
                    .where(Task.session_id == child.id, Task.kind == TaskKind.RESUME)
                    .order_by(Task.id)
                )
            ).all()
        )
        chain = [t for t in chain if root_of(t) == root.id]
        if failed := [t for t in chain if t.status == TaskStatus.FAILED]:
            return ThreadState("failed", failed[-1].error or "failed", None)
        open_tasks = [t for t in chain if t.status not in _TERMINAL]
        if open_tasks:
            return ThreadState("running", "", open_tasks[-1].id)
        return ThreadState("done", await answer_of(s, root.id), None)

    async def suspend(
        self, s: AsyncSession, task: Task, session: Session, deferred: DeferredToolRequests, reply_to: ReplyTo
    ) -> Task:
        """这一轮以待完成的工具调用结束：写 waiting 的 resume 任务、等待记录，并执行 ask 的子任务投递。"""
        resume = self._queue.enqueue(
            s,
            kind=TaskKind.RESUME,
            lane=turn_lane(session.id),
            agent_id=session.agent_id,
            session_id=session.id,
            payload={"turn_task_id": root_of(task), "reply_to": reply_to},
            waiting=True,
        )
        await s.flush()
        # 等这一轮结果的人，改为等新的 resume 任务
        await retarget_waits(s, task.id, resume.id)

        resolved_now: list[tuple[int, str | None, str | None]] = []
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
                        parent=session,
                        agent=agent,
                        thread=meta["thread"],
                        prompt=meta["message"],
                        private=True,
                        reply_to=ReplyTo.AGENT,
                    )
                    await s.flush()
                    s.add(
                        Wait(
                            waiter_task_id=resume.id, tool_call_id=call.tool_call_id, target_task_id=target.id
                        )
                    )
                case "wait":
                    target_id = int(meta["target_task_id"])
                    s.add(
                        Wait(
                            waiter_task_id=resume.id, tool_call_id=call.tool_call_id, target_task_id=target_id
                        )
                    )
                    target = await s.get(Task, target_id)
                    if target is None:
                        resolved_now.append((target_id, None, "the awaited thread was deleted"))
                    elif target.status in _TERMINAL:
                        # 工具判断「仍在运行」之后、本次提交之前目标已经结束：就地补上结果
                        if target.status == TaskStatus.FAILED:
                            resolved_now.append((target_id, None, target.error or "failed"))
                        else:
                            resolved_now.append((target_id, await answer_of(s, root_of(target)), None))
                case other:
                    raise RuntimeError(f"unknown deferred action {other!r} on {call.tool_name}")
        await s.flush()
        for target_id, result, error in resolved_now:
            await resolve_waits(s, self._queue, target_id, result=result, error=error)
        log.info(
            "turn suspended",
            extra={"session_id": session.id, "resume_task_id": resume.id, "calls": len(deferred.calls)},
        )
        return resume

    async def collected(self, s: AsyncSession, resume: Task) -> dict[str, str]:
        """resume 任务恢复时交给模型的工具结果：tool_call_id → 文本（失败时为错误说明）。"""
        waits: Sequence[Wait] = (await s.scalars(select(Wait).where(Wait.waiter_task_id == resume.id))).all()
        results: dict[str, str] = {}
        for w in waits:
            if w.result is not None:
                results[w.tool_call_id] = w.result
            elif w.error is not None:
                results[w.tool_call_id] = f"[failed] {w.error}"
            else:
                raise RuntimeError(f"resume task {resume.id} started before wait {w.id} was resolved")
        return results
