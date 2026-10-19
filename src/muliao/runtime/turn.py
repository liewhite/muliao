"""一轮对话的执行：组装上下文、运行模型、在一个事务里提交消息、计数、反思、压缩与通知。

两个调用方共用 TurnRunner：
- 队列里的 turn task（TurnHandler）：用户消息、触发器、delegate_task；
- ask_agent：在调用方这一轮里直接跑完私有 thread 的一轮（run_inline），提交时补记一条已完成的 turn task。

一轮是原子的：成功才提交消息；失败不留任何记录，由调用方按自己的失败语义处理（队列重试，或随调用方这一轮失败）。
"""

import asyncio
import weakref
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.delivery.base import Notification, Notifier
from muliao.models import Agent, Message, ReplyTo, Session, Task, TaskKind
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import CompactionScheduler, load_context
from muliao.runtime.redaction import redact_messages
from muliao.runtime.runner import AgentRuntime, RunProfile
from muliao.runtime.tasks import Commit, TaskQueue, reflection_lane, turn_lane


@dataclass(frozen=True)
class TurnOutput:
    session_id: str
    agent_id: str
    answer: str
    messages: list[ModelMessage]


async def _load(s: AsyncSession, session_id: str) -> tuple[Session, Agent]:
    session = await s.get(Session, session_id)
    if session is None:
        raise RuntimeError(f"session {session_id} vanished during its turn")
    agent = await s.get(Agent, session.agent_id)
    if agent is None:
        raise RuntimeError(f"session {session_id} references missing agent {session.agent_id}")
    return session, agent


class TurnRunner:
    def __init__(
        self,
        db: Database,
        runtime: AgentRuntime,
        profile: RunProfile,
        queue: TaskQueue,
        notifier: Notifier,
        compaction: CompactionScheduler,
    ):
        self._db = db
        self._runtime = runtime
        self._profile = profile
        self._queue = queue
        self._notifier = notifier
        self._compaction = compaction
        # 内联运行的私有 thread 不走队列 lane：同一 thread 的并行 ask 在这里串行（进程内，单进程部署）
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def run(self, session_id: str, prompt: str, reply_to: ReplyTo) -> TurnOutput:
        async with self._db.transaction() as s:
            session, agent = await _load(s, session_id)
            context = await load_context(s, session.id)
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin, reply_to=reply_to)
        result = await self._runtime.run(
            agent, scope, prompt, context.messages, self._profile, summary=context.summary
        )
        return TurnOutput(session.id, agent.id, result.output, redact_messages(result.new_messages()))

    async def commit_in(self, s: AsyncSession, turn_id: int, out: TurnOutput, reply_to: ReplyTo) -> None:
        """在调用方事务里提交一轮的全部结果。"""
        session, agent = await _load(s, out.session_id)
        s.add_all(
            Message(session_id=session.id, task_id=turn_id, payload=dump_message(m)) for m in out.messages
        )
        await s.flush()
        session.updated_at = utcnow()
        session.unread = not session.private
        await self._count_turn(s, agent)
        await self._compaction.after_turn(s, session, agent.model, out.messages)
        if reply_to == ReplyTo.USER:
            self._notifier.enqueue_in(
                s,
                agent.id,
                Notification(title=f"{agent.name} · {session.title}", body=out.answer, session_id=session.id),
            )

    async def run_inline(self, session_id: str, prompt: str, reply_to: ReplyTo) -> str:
        """在当前协程里跑完一轮并提交，返回回答。只用于私有 thread（用户不能插话，lane 里没有排队的 turn）。"""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            out = await self.run(session_id, prompt, reply_to)
            async with self._db.transaction() as s:
                turn = self._queue.record_done(
                    s,
                    kind=TaskKind.TURN,
                    lane=turn_lane(session_id),
                    agent_id=out.agent_id,
                    session_id=session_id,
                    payload={"prompt": prompt, "reply_to": reply_to},
                )
                await s.flush()
                await self.commit_in(s, turn.id, out, reply_to)
        return out.answer

    async def _count_turn(self, s: AsyncSession, agent: Agent) -> None:
        """agent 级 turn 计数；达到阈值时把 (检查点, 当前最大消息 id] 交给反思。"""
        agent.turns_since_reflection += 1
        if agent.turns_since_reflection < agent.reflect_every:
            return
        to_id = await s.scalar(
            select(func.max(Message.id))
            .join(Session, Session.id == Message.session_id)
            .where(Session.agent_id == agent.id)
        )
        self._queue.enqueue(
            s,
            kind=TaskKind.REFLECTION,
            lane=reflection_lane(agent.id),
            agent_id=agent.id,
            session_id=None,
            payload={"from_message_id": agent.reflection_checkpoint, "to_message_id": to_id},
        )
        agent.reflection_checkpoint = to_id
        agent.turns_since_reflection = 0


class TurnHandler:
    """队列里的 turn task。"""

    def __init__(self, runner: TurnRunner):
        self._runner = runner

    async def execute(self, task: Task) -> Commit:
        if task.session_id is None:
            raise RuntimeError(f"turn task {task.id} has no session")
        reply_to = ReplyTo(task.payload["reply_to"])
        out = await self._runner.run(task.session_id, task.payload["prompt"], reply_to)

        async def commit(s: AsyncSession) -> None:
            await self._runner.commit_in(s, task.id, out, reply_to)

        return commit
