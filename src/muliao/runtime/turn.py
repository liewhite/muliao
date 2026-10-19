"""一轮对话的执行：组装上下文、运行模型、在一个事务里提交消息、计数、反思、压缩与通知。

两个调用方共用 TurnRunner：
- 队列里的 turn task（TurnHandler）：用户消息、触发器、delegate_task；
- ask_agent：在调用方这一轮里直接跑完私有 thread 的一轮（run_inline），提交时补记一条已完成的 turn task。

一轮是原子的：成功才提交消息；失败不留任何记录，由调用方按自己的失败语义处理（队列重试，或随调用方这一轮失败）。
"""

import asyncio
import logging
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic_ai import DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.delivery.base import Notification, Notifier
from muliao.files import FileStore
from muliao.models import Agent, Message, ReplyTo, Session, Task, TaskKind
from muliao.runtime.answers import Answer
from muliao.runtime.attachments import copy_to_workspace, references
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import CompactionScheduler, load_context
from muliao.runtime.questions import (
    ASK_USER,
    deferred_results,
    pending_questions,
    questions_of,
    waiting_text,
)
from muliao.runtime.redaction import redact_messages
from muliao.runtime.runner import AgentRuntime, ToolGroups
from muliao.runtime.tasks import Commit, TaskQueue, reflection_lane, turn_lane

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnOutput:
    session_id: str
    agent_id: str
    answer: str
    messages: list[ModelMessage]


def _deferred(history: Sequence[ModelMessage], answers: Mapping[str, object]) -> DeferredToolResults | None:
    """历史里还挂着的提问都要在这一轮得到结果：用户的回答，或「没回答」。回答只能针对挂起的提问（receive 已校验）。"""
    pending = pending_questions(history)
    if stray := set(answers) - set(pending):
        # receive 只在没有会先执行的轮次时接受回答；走到这里是重试一个失败的回答，而之后又跑过别的轮次关闭了提问
        raise RuntimeError(
            f"answers to {sorted(stray)} are void: a later turn already closed these questions as unanswered"
        )
    return DeferredToolResults(calls=deferred_results(pending, answers)) if pending else None


def _answer_text(output: str | DeferredToolRequests) -> str:
    """一轮的回答；停在提问上时是问题列表（用于通知与 ask_agent 之外的展示）。"""
    if isinstance(output, str):
        return output
    if output.approvals or any(c.tool_name != ASK_USER for c in output.calls):
        # 只有 ask_user 会挂起；其他挂起说明工具与这里的约定不一致
        raise RuntimeError(
            f"turn deferred unexpected tool calls: calls={[c.tool_name for c in output.calls]} "
            f"approvals={[c.tool_name for c in output.approvals]}"
        )
    return waiting_text([q for c in output.calls for q in questions_of(c)])


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
        tool_groups: ToolGroups,
        queue: TaskQueue,
        notifier: Notifier,
        compaction: CompactionScheduler,
        files: FileStore,
    ):
        self._files = files
        self._db = db
        self._runtime = runtime
        self._tool_groups = tool_groups
        self._queue = queue
        self._notifier = notifier
        self._compaction = compaction
        # 内联运行的私有 thread 不走队列 lane：同一 thread 的并行 ask 在这里串行（进程内，单进程部署）
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def run(
        self,
        session_id: str,
        prompt: str,
        attachments: Sequence[str],
        answers: Mapping[str, object],
        reply_to: ReplyTo,
    ) -> TurnOutput:
        """answers：用户对挂起提问的回答（tool_call_id -> 交给模型的结果，已在 receive 校验），见 runtime.questions。"""
        async with self._db.transaction() as s:
            session, agent = await _load(s, session_id)
            context = await load_context(s, session.id)
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin, reply_to=reply_to)
        files = await self._files.referenced(attachments)
        await copy_to_workspace(self._files, files, scope.workspace)
        content = [*([prompt] if prompt.strip() else []), *references(files)]
        # 只回答了提问的一轮没有新的用户消息
        user_prompt = (content if files else prompt) if content else None
        result = await self._runtime.run(
            agent,
            scope,
            user_prompt,
            context.messages,
            self._tool_groups.profile(agent.id, agent.tool_groups),
            summary=context.summary,
            deferred=_deferred(context.messages, answers),
        )
        return TurnOutput(
            session.id, agent.id, _answer_text(result.output), redact_messages(result.new_messages())
        )

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

    async def run_inline(self, session_id: str, prompt: str) -> Answer:
        """在当前协程里跑完私有 thread 的一轮并提交，返回给发问 agent 的回答（连同这一轮挂给用户的文件）。

        只用于私有 thread：用户不能插话，lane 里没有排队的 turn；同一 thread 的并行调用由进程内的锁串行。
        """
        reply_to = ReplyTo.AGENT
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            started = utcnow()
            log.info("inline turn started", extra={"session_id": session_id})
            try:
                out = await self.run(session_id, prompt, (), {}, reply_to)
                async with self._db.transaction() as s:
                    # 先确认 thread 还在：运行期间被删除时给出可定位的错误，而不是外键冲突
                    await _load(s, session_id)
                    turn = self._queue.record_done(
                        s,
                        kind=TaskKind.TURN,
                        lane=turn_lane(session_id),
                        agent_id=out.agent_id,
                        session_id=session_id,
                        payload={"prompt": prompt, "reply_to": reply_to, "attachments": [], "answers": {}},
                        started_at=started,
                    )
                    await s.flush()
                    await self.commit_in(s, turn.id, out, reply_to)
            except Exception as e:
                log.warning("inline turn failed: %r", e, extra={"session_id": session_id})
                raise
        log.info("inline turn done", extra={"session_id": session_id, "task_id": turn.id})
        return Answer.of_run(out.answer, out.messages)

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
        out = await self._runner.run(
            task.session_id,
            task.payload["prompt"],
            task.payload["attachments"],
            task.payload["answers"],
            reply_to,
        )

        async def commit(s: AsyncSession) -> None:
            await self._runner.commit_in(s, task.id, out, reply_to)

        return commit
