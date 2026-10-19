"""TurnHandler：执行 session 的一轮对话（turn 与挂起后的 resume），并在同一事务里提交消息、计数、反思与通知。

一轮可能以待完成的工具调用（ask_agent / wait_for）结束：此时只提交消息并挂起，等结果齐了由 resume 任务接着跑，
直到模型给出最终回答。消息都记在这一轮的 root turn 名下（见 runtime/conversation.py）。

一轮是原子的：失败的 turn 不留下消息；挂起后 resume 最终失败，则整轮回滚——删除这一轮已提交的消息
（否则历史里会留下没有结果的工具调用，之后每一轮都会被模型 API 拒绝），并把 root turn 标为失败，
用户重试的是整轮（root turn 的 payload 里有原始 prompt），而不是无法单独重放的 resume。
"""

import logging

from pydantic_ai import DeferredToolRequests
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.delivery.base import Notification, Notifier
from muliao.models import Agent, Message, ReplyTo, Session, Task, TaskKind, TaskStatus
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import CompactionScheduler, load_context
from muliao.runtime.conversation import Threads, resolve_waits, root_of
from muliao.runtime.redaction import redact_messages
from muliao.runtime.runner import AgentRuntime, RunProfile
from muliao.runtime.tasks import Commit, TaskQueue, reflection_lane

log = logging.getLogger(__name__)


async def _load(s: AsyncSession, task: Task) -> tuple[Session, Agent]:
    session = await s.get(Session, task.session_id)
    agent = await s.get(Agent, task.agent_id)
    if session is None or agent is None:
        raise RuntimeError(
            f"turn task {task.id} references missing session/agent ({task.session_id}, {task.agent_id})"
        )
    return session, agent


class TurnHandler:
    """处理 TaskKind.TURN 与 TaskKind.RESUME。"""

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
            match task.kind:
                case TaskKind.TURN:
                    prompt, deferred_results = task.payload["prompt"], None
                case TaskKind.RESUME:
                    prompt, deferred_results = None, await self._threads.collected(s, task)
                case other:
                    raise RuntimeError(f"TurnHandler cannot run task {task.id} of kind {other}")
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin, reply_to=reply_to)
        result = await self._runtime.run(
            agent,
            scope,
            prompt,
            context.messages,
            self._profile,
            summary=context.summary,
            deferred_results=deferred_results,
        )
        new_messages = redact_messages(result.new_messages())
        output = result.output
        root = root_of(task)

        async def commit(s: AsyncSession) -> None:
            session, agent = await _load(s, task)
            s.add_all(
                Message(session_id=session.id, task_id=root, payload=dump_message(m)) for m in new_messages
            )
            await s.flush()
            session.updated_at = utcnow()
            if isinstance(output, DeferredToolRequests):
                await self._threads.suspend(s, task, session, output, reply_to)
                return
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

        return commit

    async def on_failed(self, s: AsyncSession, task: Task) -> None:
        if task.kind == TaskKind.RESUME:
            root = await s.get(Task, root_of(task))
            if root is None:
                raise RuntimeError(f"resume task {task.id} references missing turn {root_of(task)}")
            await s.execute(delete(Message).where(Message.task_id == root.id))
            root.status = TaskStatus.FAILED
            root.error = f"resumed after waiting, then failed (task {task.id}): {task.error}"
            log.warning(
                "suspended turn rolled back",
                extra={"session_id": task.session_id, "turn_task_id": root.id, "resume_task_id": task.id},
            )
        await resolve_waits(s, self._queue, task.id, error=task.error or "failed")

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
