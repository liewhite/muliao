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
from datetime import datetime

from pydantic_ai import AgentRunResult, DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelMessage, UserContent
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.bus import EventBus, LiveRuns, task_event
from muliao.conventions import mark_events
from muliao.db import Database, utcnow
from muliao.delivery.base import Notifier
from muliao.files import FileStore
from muliao.models import Agent, Message, Notification, ReplyTo, Session, Task, TaskKind, TaskStatus
from muliao.runtime.answers import Answer
from muliao.runtime.attachments import copy_to_workspace, references
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import CompactionScheduler, SessionContext, load_context
from muliao.runtime.conversation import TurnPayload
from muliao.runtime.events import TurnEvents, consume_in, dispatch_in, take_in
from muliao.runtime.questions import (
    ASK_USER,
    AskResult,
    deferred_results,
    pending_questions,
    questions_of,
    waiting_text,
)
from muliao.runtime.redaction import redact_messages
from muliao.runtime.runner import AgentRuntime, RunCapture, ToolGroups
from muliao.runtime.scope import RunScope
from muliao.runtime.stopping import StoppableRuns, Stopped, stopped_messages, stopped_tool_calls
from muliao.runtime.tasks import Commit, TaskQueue, reflection_lane, turn_lane

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedTurn:
    """执行前准备好的一轮：会话、agent、运行上下文、交给模型的输入。"""

    session: Session
    agent: Agent
    scope: RunScope
    context: SessionContext
    # 交给模型的用户消息；只回答了提问的一轮没有新的用户消息
    user_prompt: str | Sequence[UserContent] | None
    # 挂起提问的结果（用户的回答或「没回答」）；没有挂起的提问时为 None
    deferred: DeferredToolResults | None
    # 这一轮带上的会话事件（runtime.events），与它们在用户消息内容里的下标
    events: TurnEvents
    event_indices: tuple[int, ...]
    # 这一轮的回答交给谁：轮次自己的去向，或带上的事件要求推送给用户
    reply_to: ReplyTo


@dataclass(frozen=True)
class TurnOutput:
    session_id: str
    agent_id: str
    answer: str
    messages: list[ModelMessage]
    reply_to: ReplyTo
    # 这一轮处理掉的会话事件，提交时记下
    event_ids: tuple[int, ...]
    # 用户停止了这一轮：messages 是停止前已经产生的部分（见 stopping.stopped_messages）
    stopped: bool = False


def _deferred(
    history: Sequence[ModelMessage], answers: Mapping[str, AskResult]
) -> DeferredToolResults | None:
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
        live: LiveRuns,
        bus: EventBus,
    ):
        self._live = live
        # 内联轮次不经 worker，自己发 task 事件（前端靠它在一轮结束后重新拉取）
        self._bus = bus
        self._files = files
        # 正在运行的一轮，按会话；用户可以停止（见 runtime.stopping）
        self._runs: StoppableRuns[AgentRunResult[str | DeferredToolRequests]] = StoppableRuns()
        self._db = db
        self._runtime = runtime
        self._tool_groups = tool_groups
        self._queue = queue
        self._notifier = notifier
        self._compaction = compaction
        # 内联运行的私有 thread 不走队列 lane：同一 thread 的并行 ask 在这里串行（进程内，单进程部署）
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def run(self, session_id: str, payload: TurnPayload, received_at: datetime) -> TurnOutput:
        """执行一轮：准备输入、运行模型（用户可以停止）、整理结果。payload 的 answers 已在 receive 校验过。
        received_at 是这一轮的消息到达的时间：在它之前、之后到达的会话事件分别排在用户消息的前后。"""
        turn = await self._prepare(session_id, payload, received_at)
        # 这一轮的运行事件从这里开始留存：成功时在提交事务结束（提交或回滚）时丢弃（commit_in），其余失败在这里丢弃
        self._live.begin(session_id)
        try:
            capture = RunCapture()
            result = await self._runs.run(
                session_id,
                lambda: self._runtime.run(
                    turn.agent,
                    turn.scope,
                    turn.user_prompt,
                    turn.context.messages,
                    self._tool_groups.profile(turn.agent.id, turn.agent.tool_groups),
                    summary=turn.context.summary,
                    deferred=turn.deferred,
                    capture=capture,
                ),
            )
            return self._output(turn, result, capture)
        except BaseException:
            self._live.end(session_id)
            raise

    async def _prepare(self, session_id: str, payload: TurnPayload, received_at: datetime) -> PreparedTurn:
        async with self._db.transaction() as s:
            session, agent = await _load(s, session_id)
            context = await load_context(s, session.id)
            events = await take_in(s, session.id, received_at)
        reply_to = events.reply_to(payload.reply_to)
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin, reply_to=reply_to)
        files = await self._files.referenced(payload.attachments)
        await copy_to_workspace(self._files, files, scope.workspace)
        own: list[UserContent] = [*([payload.prompt] if payload.prompt.strip() else []), *references(files)]
        content = [*events.before, *own, *events.after]
        after_start = len(events.before) + len(own)
        indices = (*range(len(events.before)), *range(after_start, after_start + len(events.after)))
        if not content and not payload.answers:
            # 投递时一定有内容：用户消息、回答，或「只有事件」的一轮在它之前不会有别的轮次取走事件（同一会话串行）
            raise RuntimeError(f"turn of session {session_id} has nothing to hand to the model")
        return PreparedTurn(
            session=session,
            agent=agent,
            scope=scope,
            context=context,
            # 只有一段纯文本时照旧交字符串；有文件或事件时是内容列表
            user_prompt=(content if files or events.ids else payload.prompt) if content else None,
            deferred=_deferred(context.messages, payload.answers),
            events=events,
            event_indices=indices,
            reply_to=reply_to,
        )

    def _output(
        self,
        turn: PreparedTurn,
        result: AgentRunResult[str | DeferredToolRequests] | Stopped,
        capture: RunCapture,
    ) -> TurnOutput:
        session_id, agent_id = turn.session.id, turn.agent.id
        if isinstance(result, Stopped):
            messages = stopped_messages(capture.new_messages(), turn.user_prompt, turn.deferred)
            log.info(
                "turn stopped by user",
                extra={
                    "session_id": session_id,
                    "agent": agent_id,
                    "saved_messages": len(messages),
                    "stopped_tool_calls": stopped_tool_calls(messages),
                },
            )
            return TurnOutput(
                session_id,
                agent_id,
                "",
                mark_events(redact_messages(messages), turn.event_indices),
                turn.reply_to,
                turn.events.ids,
                stopped=True,
            )
        return TurnOutput(
            session_id,
            agent_id,
            _answer_text(result.output),
            mark_events(redact_messages(result.new_messages()), turn.event_indices),
            turn.reply_to,
            turn.events.ids,
        )

    def stop(self, session_id: str) -> bool:
        """停止这个会话正在运行的一轮；没有在运行的返回 False。停止前已经产生的部分照常提交。"""
        return self._runs.stop(session_id)

    async def commit_in(self, s: AsyncSession, turn_id: int, out: TurnOutput) -> None:
        """在调用方事务里提交一轮的全部结果。"""
        session, agent = await _load(s, out.session_id)
        # 事务结束才交给数据库：提交后历史里有这一轮，回滚则这一轮失败，两种情况都不再显示运行中的事件
        for outcome in ("after_commit", "after_rollback"):
            event.listen(s.sync_session, outcome, lambda _s: self._live.end(out.session_id), once=True)
        s.add_all(
            Message(session_id=session.id, task_id=turn_id, payload=dump_message(m)) for m in out.messages
        )
        await s.flush()
        await consume_in(s, out.event_ids, turn_id)
        # 这一轮运行期间到达的事件：现在需要一轮来处理（这一轮停在提问上时等用户，见 events.dispatch_in）
        await dispatch_in(s, self._queue, session, committing=turn_id)
        session.updated_at = utcnow()
        session.unread = not session.private
        await self._count_turn(s, agent)
        if out.stopped:
            # 用户自己停的：不推送；停止的一轮可能没有模型响应，也就没有上下文大小可判断，下一轮再判断是否压缩
            return
        await self._compaction.after_turn(s, session, agent.model, out.messages)
        if out.reply_to == ReplyTo.USER:
            await self._notifier.enqueue_in(
                s,
                agent.id,
                Notification(title=f"{agent.name} · {session.title}", body=out.answer, session_id=session.id),
            )

    async def run_inline(self, session_id: str, prompt: str) -> Answer:
        """在当前协程里跑完私有 thread 的一轮并提交，返回给发问 agent 的回答（连同这一轮挂给用户的文件）。

        只用于私有 thread：用户不能插话，lane 里没有排队的 turn；同一 thread 的并行调用由进程内的锁串行。
        """
        payload = TurnPayload(prompt=prompt, reply_to=ReplyTo.AGENT, attachments=[], answers={})
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            started = utcnow()
            log.info("inline turn started", extra={"session_id": session_id})
            try:
                out = await self.run(session_id, payload, started)
                async with self._db.transaction() as s:
                    # 先确认 thread 还在：运行期间被删除时给出可定位的错误，而不是外键冲突
                    await _load(s, session_id)
                    turn = self._queue.record_done(
                        s,
                        kind=TaskKind.TURN,
                        lane=turn_lane(session_id),
                        agent_id=out.agent_id,
                        session_id=session_id,
                        payload=payload.dump(),
                        started_at=started,
                    )
                    await s.flush()
                    await self.commit_in(s, turn.id, out)
            except BaseException as e:
                # 运行成功、提交前失败（如 thread 已被删除）时 commit_in 没被调用到；发问方那一轮被停止或取消时，
                # 取消也从这里经过，同样要结束运行事件并通知打开着这个 thread 的页面
                self._live.end(session_id)
                log.warning("inline turn failed: %r", e, extra={"session_id": session_id})
                self._publish_inline(None, None, session_id, TaskStatus.FAILED, f"{type(e).__name__}: {e}")
                raise
        log.info("inline turn done", extra={"session_id": session_id, "task_id": turn.id})
        self._publish_inline(turn.id, out.agent_id, session_id, TaskStatus.DONE, None)
        return Answer.of_run(out.answer, out.messages)

    def _publish_inline(
        self,
        task_id: int | None,
        agent_id: str | None,
        session_id: str,
        status: TaskStatus,
        error: str | None,
    ) -> None:
        self._bus.publish(
            task_event(
                task_id=task_id,
                kind=TaskKind.TURN,
                status=status,
                session_id=session_id,
                agent_id=agent_id,
                error=error,
            )
        )

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
        payload = TurnPayload.of(task)
        out = await self._runner.run(task.session_id, payload, task.created_at)

        async def commit(s: AsyncSession) -> None:
            await self._runner.commit_in(s, task.id, out)

        return commit
