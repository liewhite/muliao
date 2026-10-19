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
from dataclasses import dataclass, replace

from pydantic_ai import AgentRunResult, DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserContent, UserPromptPart
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.bus import LiveRuns
from muliao.db import Database, utcnow
from muliao.delivery.base import Notification, Notifier
from muliao.files import FileStore
from muliao.models import Agent, Message, ReplyTo, Session, Task, TaskKind
from muliao.runtime.answers import Answer
from muliao.runtime.attachments import copy_to_workspace, references
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import CompactionScheduler, load_context
from muliao.runtime.conversation import TurnPayload
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
from muliao.runtime.tasks import Commit, TaskQueue, reflection_lane, turn_lane
from muliao.transcript import TURN_STOPPED, unanswered_calls

log = logging.getLogger(__name__)


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


@dataclass(frozen=True)
class TurnOutput:
    session_id: str
    agent_id: str
    answer: str
    messages: list[ModelMessage]
    # 用户停止了这一轮：messages 是停止前已经产生的部分（见 _stopped_messages）
    stopped: bool = False


# 停止时还没出结果的工具调用的结果：工具的副作用可能已经发生，告诉模型它没有完成
STOPPED_TOOL_RESULT = "Stopped by the user before this tool call finished; it may have partly run."


def _stopped_messages(
    new: list[ModelMessage] | None, user_prompt: str | Sequence[UserContent] | None
) -> list[ModelMessage]:
    """用户停止的一轮要保存的消息：停止前已经产生的部分（工具已经执行过，下一轮的模型要知道），没出结果的调用补上
    结果，最后一条标上 TURN_STOPPED。正在生成的那段回答由 pydantic-ai 以 state='interrupted' 留在运行历史里，一起保存，
    其中参数还没生成完的工具调用同样补上结果（参数在落库前脱敏，截断的 vault_set 参数整体替换，见 redaction）。
    还没开始第一次模型请求时只有用户这条消息。"""
    if new is None:
        new = [] if user_prompt is None else [ModelRequest(parts=[UserPromptPart(user_prompt)])]
    messages = list(new)
    if dangling := unanswered_calls(messages):
        messages.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(c.tool_name, STOPPED_TOOL_RESULT, tool_call_id=c.tool_call_id)
                    for c in dangling
                ]
            )
        )
    if messages:
        last = messages[-1]
        messages[-1] = replace(last, metadata={**(last.metadata or {}), TURN_STOPPED: True})
    return messages


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
    ):
        self._live = live
        self._files = files
        # 正在运行的一轮（模型运行的子任务），按会话；用户停止时取消它
        self._stoppable: dict[str, asyncio.Task[AgentRunResult[str | DeferredToolRequests]]] = {}
        self._db = db
        self._runtime = runtime
        self._tool_groups = tool_groups
        self._queue = queue
        self._notifier = notifier
        self._compaction = compaction
        # 内联运行的私有 thread 不走队列 lane：同一 thread 的并行 ask 在这里串行（进程内，单进程部署）
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def run(self, session_id: str, payload: TurnPayload) -> TurnOutput:
        """payload 是这一轮的输入；其中的 answers 已在 receive 校验过（见 conversation.TurnPayload）。"""
        prompt, reply_to = payload.prompt, payload.reply_to
        async with self._db.transaction() as s:
            session, agent = await _load(s, session_id)
            context = await load_context(s, session.id)
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin, reply_to=reply_to)
        files = await self._files.referenced(payload.attachments)
        await copy_to_workspace(self._files, files, scope.workspace)
        content = [*([prompt] if prompt.strip() else []), *references(files)]
        # 只回答了提问的一轮没有新的用户消息
        user_prompt = (content if files else prompt) if content else None
        # 这一轮的运行事件从这里开始留存：成功时在提交事务结束（提交或回滚）时丢弃（commit_in），其余失败在这里丢弃
        self._live.begin(session.id)
        capture = RunCapture()
        # 模型运行放在单独的子任务里：用户停止只取消它，与关停、总时限对外层任务的取消区分开（stop）
        inner = asyncio.create_task(
            self._runtime.run(
                agent,
                scope,
                user_prompt,
                context.messages,
                self._tool_groups.profile(agent.id, agent.tool_groups),
                summary=context.summary,
                deferred=_deferred(context.messages, payload.answers),
                capture=capture,
            ),
            name=f"turn-{session.id}",
        )
        if session.id in self._stoppable:
            # 同一会话同时只有一轮（lane 串行、内联轮次有锁）：已有登记说明这个前提被打破，停止会停错一轮
            inner.cancel()
            raise RuntimeError(f"session {session.id} already has a running turn")
        self._stoppable[session.id] = inner
        try:
            try:
                result = await inner
            except asyncio.CancelledError:
                if not inner.cancelled() or _cancelling():
                    # 外层被取消（关停、总时限）：这次运行一起取消，按原来的语义由队列恢复或重试
                    inner.cancel()
                    await asyncio.wait({inner})
                    if not inner.cancelled() and (error := inner.exception()) is not None:
                        log.warning(
                            "turn run failed while being cancelled: %r",
                            error,
                            extra={"session_id": session.id},
                        )
                    raise
                messages = _stopped_messages(capture.new_messages(), user_prompt)
                log.info(
                    "turn stopped by user",
                    extra={
                        "session_id": session.id,
                        "agent": agent.id,
                        "saved_messages": len(messages),
                        "stopped_tool_calls": sum(
                            1
                            for m in messages
                            for p in m.parts
                            if isinstance(p, ToolReturnPart) and p.content == STOPPED_TOOL_RESULT
                        ),
                    },
                )
                return TurnOutput(session.id, agent.id, "", redact_messages(messages), stopped=True)
            return TurnOutput(
                session.id, agent.id, _answer_text(result.output), redact_messages(result.new_messages())
            )
        except BaseException:
            self._live.end(session.id)
            raise
        finally:
            if self._stoppable.get(session.id) is inner:
                del self._stoppable[session.id]

    def stop(self, session_id: str) -> bool:
        """停止这个会话正在运行的一轮；没有在运行的返回 False。停止前已经产生的部分照常提交（见 _stopped_messages）。"""
        inner = self._stoppable.get(session_id)
        if inner is None:
            return False
        inner.cancel()
        return True

    async def commit_in(self, s: AsyncSession, turn_id: int, out: TurnOutput, reply_to: ReplyTo) -> None:
        """在调用方事务里提交一轮的全部结果。"""
        session, agent = await _load(s, out.session_id)
        # 事务结束才交给数据库：提交后历史里有这一轮，回滚则这一轮失败，两种情况都不再显示运行中的事件
        for outcome in ("after_commit", "after_rollback"):
            event.listen(s.sync_session, outcome, lambda _s: self._live.end(out.session_id), once=True)
        s.add_all(
            Message(session_id=session.id, task_id=turn_id, payload=dump_message(m)) for m in out.messages
        )
        await s.flush()
        session.updated_at = utcnow()
        session.unread = not session.private
        await self._count_turn(s, agent)
        if out.stopped:
            # 用户自己停的：不推送；停止的一轮可能没有模型响应，也就没有上下文大小可判断，下一轮再判断是否压缩
            return
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
        payload = TurnPayload(prompt=prompt, reply_to=ReplyTo.AGENT, attachments=[], answers={})
        reply_to = payload.reply_to
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            started = utcnow()
            log.info("inline turn started", extra={"session_id": session_id})
            try:
                out = await self.run(session_id, payload)
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
                    await self.commit_in(s, turn.id, out, reply_to)
            except Exception as e:
                # 运行成功、提交前失败（如 thread 已被删除）时 commit_in 没被调用到
                self._live.end(session_id)
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
        payload = TurnPayload.of(task)
        out = await self._runner.run(task.session_id, payload)

        async def commit(s: AsyncSession) -> None:
            await self._runner.commit_in(s, task.id, out, payload.reply_to)

        return commit
