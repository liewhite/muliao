"""会话的消费者：把信箱里的信件交给模型。设计见 docs/architecture.md「会话信箱」。

一次运行（一条 run task）的编排，各部分在各自的模块里：
- 起点由历史决定（_opening）：历史停在一条没得到回复、没标停止的请求上就重发它；最后一条回复里有没执行完的工具
  调用（进程在执行工具时退出）就执行它们；停在挂起的调用上时按 runtime.pending 交回结果；否则把信箱里排队的信件
  作为开头交给模型（runtime.content）。
- 逐步运行、每一步保存（runtime.steps）；上下文超过阈值时在步与步之间压缩后续跑（runtime.compaction）。
- 收尾（runtime.finish），与运行记为完成在同一个事务里。
- 用户停止（runtime.halt）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic_ai import DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserContent
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.bus import LiveRuns
from muliao.conventions import answer_text, is_stopped, unanswered_calls
from muliao.db import Database
from muliao.models import Mail, MailState, Message, Task
from muliao.platform.messaging import Messaging
from muliao.runtime.codec import load_messages
from muliao.runtime.compaction import Compactor, Verdict
from muliao.runtime.content import ContentBuilder
from muliao.runtime.conversation import failure_of
from muliao.runtime.finish import RunEnd, RunFinisher, load_session
from muliao.runtime.halt import SessionHalt
from muliao.runtime.history import load_context
from muliao.runtime.holds import called_in
from muliao.runtime.mailbox import RunPayload, discard_in, handed_to, queued_in
from muliao.runtime.pending import (
    DEFERRED_TOOLS,
    Delegated,
    answered_in,
    deferred_results,
    is_on_time,
    pending_calls,
    waiting,
)
from muliao.runtime.questions import ASK_USER, questions_of, waiting_text
from muliao.runtime.runner import AgentRuntime, ToolGroups
from muliao.runtime.scope import Audience
from muliao.runtime.session_events import Ending
from muliao.runtime.settlement import settle_in, unsettled_in
from muliao.runtime.steps import CompactionDue, RunState, Steps
from muliao.runtime.stopping import StoppableRuns, Stopped
from muliao.runtime.tasks import Commit, no_commit

log = logging.getLogger(__name__)


def resumable(history: list[ModelMessage], delegated: Delegated) -> bool:
    """历史从中途续跑：停在一条没得到回复、也没被停止的请求上，或最后的回复里有没执行完的工具调用（挂起的调用除外，
    包括没委派出去、还要再执行的 ask_agent，见 pending.pending_calls）。

    停在挂起的调用上时不是续跑，哪怕末尾是一条请求：挂起的调用与别的工具在同一个回复里时，pydantic-ai 以挂起结束时
    把别的工具的结果追加成一条请求，这时要等回答（或跳过）作为 DeferredToolResults 交回，不能把那条请求重发出去。"""
    pending = pending_calls(history, delegated)
    if not history or pending:
        return False
    last = history[-1]
    if isinstance(last, ModelRequest):
        return not is_stopped(last)
    return bool(unanswered_calls(history))


def paused_end(calls: Sequence[ToolCallPart]) -> RunEnd:
    """停在这些挂起的调用上：ask_user 的问题要告诉用户。"""
    questions = [q for c in calls if c.tool_name == ASK_USER for q in questions_of(c)]
    return RunEnd(
        answer=waiting_text(questions) if questions else "",
        paused=True,
        asked_user=bool(questions),
        stopped=False,
    )


def end_of(output: str | DeferredToolRequests) -> RunEnd:
    """模型运行的输出怎么收尾：最终回答，或停在挂起的调用上。"""
    if isinstance(output, str):
        return RunEnd(answer=output, paused=False, asked_user=False, stopped=False)
    if output.approvals or any(c.tool_name not in DEFERRED_TOOLS for c in output.calls):
        # 只有 ask_user / ask_agent 会挂起；其他挂起说明工具与这里的约定不一致
        raise RuntimeError(
            f"run deferred unexpected tool calls: calls={[c.tool_name for c in output.calls]} "
            f"approvals={[c.tool_name for c in output.approvals]}"
        )
    return paused_end(output.calls)


def settled_end(history: list[ModelMessage], delegated: Delegated) -> RunEnd:
    """之前的尝试已经跑完、只差收尾：从保存的历史得出它怎么结束的。"""
    if pending := pending_calls(history, delegated):
        return paused_end(list(pending.values()))
    last = history[-1]
    if not isinstance(last, ModelResponse):
        raise RuntimeError(f"a settled run must end with a model response, history ends with {last!r}")
    return RunEnd(answer=answer_text(last), paused=False, asked_user=False, stopped=False)


class Consumer:
    def __init__(
        self,
        db: Database,
        runtime: AgentRuntime,
        tool_groups: ToolGroups,
        live: LiveRuns,
        content: ContentBuilder,
        steps: Steps,
        finisher: RunFinisher,
        halt: SessionHalt,
        compactor: Compactor,
    ):
        self._db = db
        self._runtime = runtime
        self._tool_groups = tool_groups
        self._live = live
        self._content = content
        self._steps = steps
        self._finisher = finisher
        self._halt = halt
        self._compactor = compactor
        self._runs: StoppableRuns[Commit] = StoppableRuns(db.exclusive)
        # 正在进行的运行的状态，按会话：被停止时从这里取（运行的协程已被取消）
        self._active: dict[str, RunState] = {}

    async def run(self, run_id: int, session_id: str) -> Commit:
        """执行一次运行直到模型不再调用工具（或停在挂起的调用上、被用户停止），返回它的收尾。"""
        try:
            result = await self._runs.run(session_id, lambda: self._run(run_id, session_id))
            if isinstance(result, Stopped):
                return await self._stopped(run_id, session_id)
            return result
        finally:
            self._active.pop(session_id, None)
            self._live.end(session_id)

    async def _run(self, run_id: int, session_id: str) -> Commit:
        async with self._db.transaction() as s:
            session, agent = await load_session(s, session_id)
        # 上下文超过阈值时先压缩（之后每一步之前同样检查，见 runtime.steps.CompactionDue）
        async with self._db.transaction() as s:
            context = await load_context(s, session.id)
        verdict = self._compactor.verdict(session.id, context.messages, context.stale_responses)
        compacted = await self._compactor.compact(session, agent) if verdict == Verdict.DUE else None
        async with self._db.transaction() as s:
            context = await load_context(s, session.id)
            queued = await queued_in(s, session.id)
            earlier = list((await s.scalars(select(Mail).where(Mail.consumed_by == run_id))).all())
            delegated = await called_in(s, session.id)
            adopted = await _adopted(s, session.id, run_id) if resumable(context.messages, delegated) else []
            saved_rows = list(
                (await s.scalars(select(Message).where(Message.task_id == run_id).order_by(Message.id))).all()
            )
            last_message = await s.scalar(
                select(func.max(Message.id)).where(Message.session_id == session_id)
            )
        history = context.messages
        state = RunState(
            run_id=run_id,
            session=session,
            agent=agent,
            resending=resumable(history, delegated) and isinstance(history[-1], ModelRequest),
            handled=[*earlier, *adopted],
            messages=load_messages(r.payload for r in saved_rows),
            last_row=saved_rows[-1] if saved_rows else None,
        )
        state.stale_responses = context.stale_responses
        state.compaction_exhausted = verdict == Verdict.HOPELESS or compacted == 0
        self._active[session_id] = state
        prompt, deferred = await self._opening(state, history, queued, delegated)
        if prompt is None and deferred is None and not resumable(history, delegated):
            # 没有要交给模型的：信箱空了，或停在挂起的调用上、还不能继续。之前的尝试已经跑完、只差收尾时（它保存的就是
            # 历史的末尾），按保存的历史收尾；之后的运行已经接着它保存的部分继续了（重试一个过时的失败运行），推送与
            # 委派的结局由那些运行做过，这里只了结它自己
            if state.last_row is None or state.last_row.id != last_message:
                state.messages, state.handled = [], []
            log.info("nothing to consume", extra={"session_id": session_id, "run_id": run_id})
            return (
                self._finisher.commit(state, settled_end(history, delegated))
                if state.messages
                else self._finisher.idle(state)
            )
        # 运行开始时就定下（工具与指令随之确定）：私有 thread 的回答交给发问的 agent；否则看它处理过的、和这次要处理的
        # 信件里有没有用户在等的
        awaited = any(m.awaited for m in [*state.handled, *queued])
        audience = Audience.AGENT if session.private else Audience.USER if awaited else Audience.NONE
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin, audience=audience)
        profile = self._tool_groups.profile(agent.id, agent.tool_groups)
        self._live.begin(session_id, last_message)
        while True:
            try:
                result = await self._runtime.run_steps(
                    agent,
                    scope,
                    prompt,
                    history,
                    profile,
                    self._steps.hooks(state),
                    summary=context.summary,
                    deferred=deferred,
                )
                break
            except CompactionDue:
                # 这次请求已经保存：压缩，再从库里加载上下文（摘要 + 切点之后），从它续跑
                compacted = await self._compactor.compact(session, agent)
                async with self._db.transaction() as s:
                    context = await load_context(s, session.id)
                history, prompt, deferred = context.messages, None, None
                state.stale_responses = context.stale_responses
                state.compaction_exhausted = compacted == 0
                state.resending = True
        await self._steps.save_tail(state)
        return self._finisher.commit(state, end_of(result.output))

    async def _opening(
        self, state: RunState, history: list[ModelMessage], queued: list[Mail], delegated: Delegated
    ) -> tuple[list[UserContent] | None, DeferredToolResults | None]:
        """这次运行开头交给模型的：新信件的内容，以及挂起调用的结果。从历史续跑、或还在等挂起调用的回答时什么都不交。"""
        if resumable(history, delegated) or waiting(history, queued, delegated):
            return None, None
        pending = pending_calls(history, delegated)
        answers = answered_in(queued, pending)
        deferred = (
            DeferredToolResults(calls=deferred_results(pending, answers, delegated)) if pending else None
        )
        state.taken = list(queued)
        content = await self._content.of(state.agent.id, [m for m in queued if not is_on_time(m, pending)])
        # 开头的内容由 pydantic-ai 放进第一次请求的用户消息（下标从 0 起），事件在那次请求保存前标上
        state.opening_events = content.events
        return (content.items or None), deferred

    # ---- 停止 ----

    async def stop(self, session_id: str) -> bool:
        """停止这个会话正在进行的运行；没有在运行的返回 False（调用方仍会停止会话，见 stop_session）。"""
        return await self._runs.stop(session_id)

    async def stop_session(self, session_id: str) -> None:
        """用户停止会话：停下正在进行的运行（它在 _stopped 里收尾），没有在运行时直接停止会话（runtime.halt）。"""
        if not await self.stop(session_id):
            await self._halt(session_id)

    async def _stopped(self, run_id: int, session_id: str) -> Commit:
        state = self._active.get(session_id)
        saved = 0 if state is None else await self._steps.save_stopped(state)
        log.info("run stopped by user", extra={"session_id": session_id, "run_id": run_id, "saved": saved})
        await self._halt(session_id)
        if state is None:
            # 停在这次运行读出状态之前：它什么都没做
            return no_commit
        return self._finisher.commit(state, RunEnd("", paused=False, asked_user=False, stopped=True))


async def _adopted(s: AsyncSession, session_id: str, run_id: int) -> list[Mail]:
    """从历史中途续跑、而停下的那部分是另一次运行保存的（它失败了，之后有新信件唤醒了这次运行）：这次运行接着做完
    它的工作，也接管它处理过的信件——推送由这次运行做。之后再重试那次运行时什么都不做（Consumer._run 的「没有要
    交给模型的」）。"""
    tail_run = await s.scalar(
        select(Message.task_id).where(Message.session_id == session_id).order_by(Message.id.desc()).limit(1)
    )
    if tail_run is None or tail_run == run_id:
        return []
    return list((await s.scalars(select(Mail).where(Mail.consumed_by == tail_run))).all())


class RunHandler:
    """队列里的运行（run task）。"""

    def __init__(self, consumer: Consumer, messaging: Messaging):
        self._consumer = consumer
        self._messaging = messaging

    async def execute(self, task: Task) -> Commit:
        if task.session_id is None:
            raise RuntimeError(f"run {task.id} has no session")
        RunPayload.of(task)  # 在第一现场校验存下的 payload，不等到唤醒或失败收尾时才读到它
        return await self._consumer.run(task.id, task.session_id)

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """最终失败：交给这个会话、没能做完的委派以「失败」结清（发起方不会一直等）。按会话范围：已交给模型、还没结清的
        全部（包括早先停在挂起调用上的运行处理过的、被续跑接管的），加上交给过这次运行、还排着的——运行在第一次保存
        之前就失败时，开头取走的信件一直排着，也不会再有运行因为它们被唤醒（mailbox.handed_to）；它们随结清一起
        退出信箱，不会在之后的运行里被悄悄执行。运行开始之后才到的不动：它们会唤醒新的运行。"""
        if task.session_id is None:
            raise RuntimeError(f"run {task.id} has no session")
        stranded = [
            m for m in await unsettled_in(s, task.session_id, [MailState.QUEUED]) if handed_to(m, task)
        ]
        child, _agent = await load_session(s, task.session_id)
        settle_in(
            s,
            self._messaging,
            child,
            [*await unsettled_in(s, task.session_id, [MailState.CONSUMED]), *stranded],
            Ending.FAILED,
            failure_of(task),
        )
        await discard_in(s, task.session_id, stranded)
