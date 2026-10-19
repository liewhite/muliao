"""TurnHandler：执行 session 的一轮对话，并在同一事务里提交消息、计数、反思与通知。

一轮可能以待完成的工具调用（ask_agent / wait_for）结束：此时只提交消息并挂起，等结果齐了这个 task 再次执行，
带着结果接着跑，直到模型给出最终回答。一轮是原子的：彻底失败时整轮回滚（见 runtime/conversation.py）。
"""

from pydantic_ai import DeferredToolRequests
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.delivery.base import Notification, Notifier
from muliao.models import Agent, Message, ReplyTo, Session, Task, TaskKind, TaskStatus
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import CompactionScheduler, load_context
from muliao.runtime.conversation import Threads, collected, consume_waits, resolve_waits, roll_back_turn
from muliao.runtime.redaction import redact_messages
from muliao.runtime.runner import AgentRuntime, RunProfile
from muliao.runtime.tasks import Commit, TaskQueue, reflection_lane


async def _load(s: AsyncSession, task: Task) -> tuple[Session, Agent]:
    session = await s.get(Session, task.session_id)
    agent = await s.get(Agent, task.agent_id)
    if session is None or agent is None:
        raise RuntimeError(
            f"turn task {task.id} references missing session/agent ({task.session_id}, {task.agent_id})"
        )
    return session, agent


class TurnHandler:
    def __init__(
        self,
        db: Database,
        runtime: AgentRuntime,
        profile: RunProfile,
        queue: TaskQueue,
        notifier: Notifier,
        compaction: CompactionScheduler,
        threads: Threads,
    ):
        self._compaction = compaction
        self._db = db
        self._runtime = runtime
        self._profile = profile
        self._queue = queue
        self._notifier = notifier
        self._threads = threads

    async def execute(self, task: Task) -> Commit:
        reply_to = ReplyTo(task.payload["reply_to"])
        async with self._db.transaction() as s:
            session, agent = await _load(s, task)
            context = await load_context(s, session.id)
            # 挂起过的一轮带着所等的结果接着跑；否则从 prompt 开始
            results = await collected(s, task)
            if not results and await s.scalar(select(Message.id).where(Message.task_id == task.id).limit(1)):
                # 不变量：一轮已提交过消息，当且仅当它挂起着、有等待记录（提交与回滚都同时写/删二者）
                raise RuntimeError(f"turn {task.id} has committed messages but nothing to resume with")
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin, reply_to=reply_to)
        result = await self._runtime.run(
            agent,
            scope,
            None if results else task.payload["prompt"],
            context.messages,
            self._profile,
            summary=context.summary,
            deferred_results=results or None,
        )
        new_messages = redact_messages(result.new_messages())
        output = result.output

        async def commit(s: AsyncSession) -> TaskStatus:
            session, agent = await _load(s, task)
            s.add_all(
                Message(session_id=session.id, task_id=task.id, payload=dump_message(m)) for m in new_messages
            )
            # 用过的结果已随消息进入历史
            await consume_waits(s, task)
            await s.flush()
            session.updated_at = utcnow()
            if isinstance(output, DeferredToolRequests):
                return await self._threads.suspend(s, task, session, output)
            session.unread = not session.private
            await self._count_turn(s, agent)
            await self._compaction.after_turn(s, session, agent.model, new_messages)
            if reply_to == ReplyTo.USER:
                self._notifier.enqueue_in(
                    s,
                    agent.id,
                    Notification(title=f"{agent.name} · {session.title}", body=output, session_id=session.id),
                )
            await resolve_waits(s, self._queue, task.id, result=output)
            return TaskStatus.DONE

        return commit

    async def on_failed(self, s: AsyncSession, task: Task) -> None:
        await roll_back_turn(s, self._queue, task)

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
