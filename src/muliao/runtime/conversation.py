"""会话领域：子会话 thread、按轮的结果路由、所有新一轮对话的唯一入口。设计见 docs/architecture.md「会话领域」。

一轮对话就是一个 turn task。委派有两种：delegate_task 往公开 thread 投递一轮（走队列，调用方不等）；
ask_agent 在调用方这一轮里直接跑完私有 thread 的一轮（见 runtime/turn.TurnRunner.run_inline）。
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import utcnow
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.files import load_metas
from muliao.ids import new_id
from muliao.models import Agent, Message, Origin, ReplyTo, Session, Task, TaskKind, TaskStatus
from muliao.runtime.codec import load_messages
from muliao.runtime.questions import (
    AskResult,
    Question,
    QuestionAnswer,
    answer_result,
    pending_in_session,
)
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
    origin_ref 标识具体的发送方：触发器 id、委派方（父）会话 id；用户为 None。
    """

    origin: Origin
    origin_ref: str | None
    content: str
    session: str | NewSession
    reply_to: ReplyTo
    # 随消息发送的文件 id（muliao.files）；只有用户在界面上发的消息会带
    attachments: tuple[str, ...] = ()
    # 对这个会话里挂起提问的回答：tool_call_id -> 每题的回答（runtime.questions）；只有用户会带
    answers: Mapping[str, Sequence[QuestionAnswer]] = field(default_factory=dict)


@dataclass(frozen=True)
class Received:
    session: Session
    turn: Task


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


class TurnPayload(BaseModel):
    """turn task 的 payload：一轮的输入，在 receive（与内联轮次）里写入、执行时读出。字段只在这里定义，
    读写都经过它；存量数据由迁移补齐，所以读取时严格校验，不认识的形状就是数据与代码不一致。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str
    reply_to: ReplyTo
    # 随消息发送的文件 id（muliao.files）
    attachments: list[str]
    # 对这个会话里挂起提问的回答：tool_call_id -> 交给模型的结果（runtime.questions）
    answers: dict[str, AskResult]

    @classmethod
    def of(cls, task: Task) -> "TurnPayload":
        if task.kind != TaskKind.TURN:
            raise RuntimeError(f"task {task.id} is a {task.kind} task, not a turn")
        return cls.model_validate(task.payload)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---- 回答挂起的提问：回答必须紧接在提问那一轮后面执行（取决于这个会话里各轮的排队顺序） ----


def answering_blocked(open_turns: Sequence[Task]) -> bool:
    """回答必须紧接在提问那一轮后面执行。排队或运行中的轮次会先跑完并把提问记为没回答；失败待重试的回答
    还占着这次提问。这两种情况下不接受回答（界面也不显示表单），失败的回答只能重试。"""
    return any(
        t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        or (t.status == TaskStatus.FAILED and TurnPayload.of(t).answers)
        for t in open_turns
    )


async def answerable_questions(s: AsyncSession, session_id: str) -> dict[str, list[Question]] | None:
    """这个会话现在能回答的提问；有会先执行的轮次或失败待重试的回答时为 None（见 answering_blocked）。
    界面展示与 receive 校验都经过这里。"""
    if answering_blocked(await _open_turns(s, session_id)):
        return None
    return await pending_in_session(s, session_id)


async def _open_turns(s: AsyncSession, session_id: str) -> list[Task]:
    rows = await s.scalars(
        select(Task).where(
            Task.session_id == session_id, Task.kind == TaskKind.TURN, Task.status != TaskStatus.DONE
        )
    )
    return list(rows.all())


async def _resolve_answers(
    s: AsyncSession, session_id: str, answers: Mapping[str, Sequence[QuestionAnswer]]
) -> dict[str, AskResult]:
    """用户提交的回答：会话里没有会先执行的轮次，且每个都对应还在等回答的提问。返回交给模型的结果。"""
    pending = await answerable_questions(s, session_id)
    if pending is None:
        raise Conflict(
            f"session {session_id} has a turn that runs first (or a failed answer to retry); "
            "it would close these questions, so they can no longer be answered"
        )
    resolved = {}
    for call_id, given in answers.items():
        if call_id not in pending:
            raise Conflict(f"question {call_id!r} is not waiting for an answer in session {session_id}")
        resolved[call_id] = answer_result(pending[call_id], given)
    return resolved


async def receive(s: AsyncSession, queue: TaskQueue, inbound: Inbound) -> Received:
    """所有新一轮对话的唯一入口：确定会话（继续或新建），投递一轮。"""
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
    answers = await _resolve_answers(s, session.id, inbound.answers) if inbound.answers else {}
    session.updated_at = utcnow()
    turn = queue.enqueue(
        s,
        kind=TaskKind.TURN,
        lane=turn_lane(session.id),
        agent_id=session.agent_id,
        session_id=session.id,
        payload=TurnPayload(
            prompt=inbound.content,
            reply_to=inbound.reply_to,
            attachments=list(inbound.attachments),
            answers=answers,
        ).dump(),
    )
    log.info(
        "message received",
        extra={"session_id": session.id, "origin": inbound.origin, "origin_ref": inbound.origin_ref},
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


async def _queue_session(s: AsyncSession, session: Session) -> Session:
    """承载这个会话执行的、走队列的会话：私有 thread 的轮次只在它最近的非私有祖先那一轮里内联运行。"""
    while session.private:
        if session.parent_session_id is None:
            raise RuntimeError(f"private thread {session.id} has no parent")
        parent = await s.get(Session, session.parent_session_id)
        if parent is None:
            raise RuntimeError(
                f"private thread {session.id} references missing parent {session.parent_session_id}"
            )
        session = parent
    return session


async def delete_sessions(s: AsyncSession, roots: list[Session]) -> None:
    """删除会话的唯一规则（删会话、删 agent 都经过这里）。

    私有 thread 只对父会话有意义，逐层随之删除；用户可见的 thread 保留为独立会话（parent 置空）。
    任一被删会话正在执行时拒绝，避免运行结果无处提交、工具写到被删的目录。私有 thread 的内联运行不留
    running 记录，所以看承载它的、走队列的祖先会话（偏保守：祖先在跑任何一轮都视为它可能在跑）。
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
        if running := await running_task_ids(s, session_id=(await _queue_session(s, session)).id):
            # 错误也会交还给模型，不带 id；定位信息记在日志里
            log.info(
                "session deletion refused: tasks running", extra={"session_id": session.id, "tasks": running}
            )
            what = "the session" if session.id in {x.id for x in roots} else "one of its private threads"
            raise Conflict(f"{what} has a task running right now; wait for it to finish, then delete")
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
    # idle：还没有任何一轮的记录。私有 thread 的内联轮次只在完成时记录，所以只会是 done 或 idle
    # （第一次 ask 还在跑、或失败了，都是 idle；续聊还在跑时报告的是上一轮）
    status: Literal["done", "failed", "running", "idle"]
    # done 时为最终回答，failed 时为错误说明，其余为空
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
        delegate_task（公开 thread 的轮次走队列、用户也能插话，不能在调用方这一轮里直接跑）。
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
    ) -> Received:
        """在公开 thread 里投递一轮（thread 不存在则新建）；回答推送给用户，调用方不等。返回投递到的会话与这一轮。"""
        child = await self.open(s, parent_id=parent_id, agent=agent, thread=thread, private=False)
        inbound = Inbound(
            origin=Origin.DELEGATION,
            origin_ref=parent_id,
            content=prompt,
            session=child.id,
            reply_to=ReplyTo.USER,
        )
        return await receive(s, self._queue, inbound)

    async def state(self, s: AsyncSession, child: Session) -> ThreadState:
        """thread 最近一轮的状态。"""
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
                return ThreadState("done", await answer_of(s, turn.id))
            case TaskStatus.FAILED:
                return ThreadState("failed", failure_of(turn))
            case _:
                return ThreadState("running", "")
