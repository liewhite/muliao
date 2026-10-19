"""TurnHandler：执行 session 的一轮对话，并在同一事务里提交消息、计数、反思与通知。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.delivery.base import Notification, Notifier
from muliao.models import Agent, DeliveryChannel, Message, Session, Task, TaskKind
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import CompactionTrigger, load_context
from muliao.runtime.redaction import redact_messages
from muliao.runtime.runner import AgentRuntime, RunProfile
from muliao.runtime.tasks import Commit, TaskQueue, reflection_lane


async def _load(s: AsyncSession, task: Task) -> tuple[Session, Agent]:
    session = await s.get(Session, task.session_id)
    agent = await s.get(Agent, task.agent_name)
    if session is None or agent is None:
        raise RuntimeError(
            f"turn task {task.id} references missing session/agent ({task.session_id}, {task.agent_name})"
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
        compaction: CompactionTrigger,
    ):
        self._compaction = compaction
        self._db = db
        self._runtime = runtime
        self._profile = profile
        self._queue = queue
        self._notifier = notifier

    async def execute(self, task: Task) -> Commit:
        async with self._db.transaction() as s:
            session, agent = await _load(s, task)
            context = await load_context(s, session.id)
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin)
        result = await self._runtime.run(
            agent, scope, task.payload["prompt"], context.messages, self._profile, summary=context.summary
        )
        new_messages = redact_messages(result.new_messages())

        async def commit(s: AsyncSession) -> None:
            session, agent = await _load(s, task)
            s.add_all(
                Message(session_id=session.id, task_id=task.id, payload=dump_message(m)) for m in new_messages
            )
            await s.flush()
            session.unread = True
            session.updated_at = utcnow()
            await self._count_turn(s, agent)
            self._compaction.maybe_enqueue(s, session, new_messages)
            if session.delivery != DeliveryChannel.NONE:
                self._notifier.enqueue_in(
                    s,
                    agent.name,
                    Notification(
                        channel=session.delivery,
                        target=session.delivery_target,
                        title=f"{agent.name} · {session.title}",
                        body=result.output,
                        session_id=session.id,
                    ),
                )

        return commit

    async def _count_turn(self, s: AsyncSession, agent: Agent) -> None:
        """agent 级 turn 计数；达到阈值时把 (检查点, 当前最大消息 id] 交给反思。"""
        agent.turns_since_reflection += 1
        if agent.turns_since_reflection < agent.reflect_every:
            return
        to_id = await s.scalar(
            select(func.max(Message.id))
            .join(Session, Session.id == Message.session_id)
            .where(Session.agent_name == agent.name)
        )
        self._queue.enqueue(
            s,
            kind=TaskKind.REFLECTION,
            lane=reflection_lane(agent.name),
            agent_name=agent.name,
            session_id=None,
            payload={"from_message_id": agent.reflection_checkpoint, "to_message_id": to_id},
        )
        agent.reflection_checkpoint = to_id
        agent.turns_since_reflection = 0
