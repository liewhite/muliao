"""会话的消费者：把信箱里的信件交给模型。设计见 docs/architecture.md「会话信箱」。

一次运行（一条 run task）的编排，各部分在各自的模块里：
- 起点由历史决定（_opening）：历史停在一条没得到回复、没标停止的请求上就重发它；最后一条回复里有没出结果的工具
  调用（这一步在执行工具时被打断）就先补上「被中断、没有重新执行」的结果（sessions.history.interrupted_results），
  再重发那条补出来的请求，不重新执行它们——可以安全重放的那一步除外（interrupted_calls）；停在挂起的调用上时按
  sessions.pending 交回结果（同一个回复里被打断的别的调用同样先补上结果）；否则把信箱里排队的信件作为开头交给
  模型（sessions.content）。
- 逐步运行、每一步保存（sessions.steps）；上下文超过阈值时在步与步之间压缩后续跑（sessions.compaction）。
- 收尾（sessions.finish），与运行记为完成在同一个事务里。
- 用户停止（sessions.halt）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserContent
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import (
    AgentRuntime,
    Finished,
    Paused,
    ToolGroups,
    answer_text,
    is_stopped,
    unanswered_calls,
)
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, TaskQueue, no_commit
from muliao.sessions.codec import load_messages
from muliao.sessions.compaction import Compactor, Verdict
from muliao.sessions.content import ContentBuilder
from muliao.sessions.deferred import DEFERRED_TOOLS, questions_in, replayable
from muliao.sessions.events import RunFailed
from muliao.sessions.finish import RunEnd, RunFinisher
from muliao.sessions.halt import Halted, SessionHalt
from muliao.sessions.history import interrupted_results, latest_compaction, load_context
from muliao.sessions.holds import called_in
from muliao.sessions.kinds import policy_of
from muliao.sessions.live import LiveRuns
from muliao.sessions.loading import load_session
from muliao.sessions.mailbox import RunPayload, hand_over_in, queued_in, wake_in
from muliao.sessions.origins import origin_note
from muliao.sessions.pending import (
    Delegated,
    answered_in,
    deferred_results,
    is_on_time,
    pending_calls,
    waiting,
)
from muliao.sessions.questions import waiting_text
from muliao.sessions.settlement import settle_failure_in
from muliao.sessions.steps import CompactionDue, RunState, Steps
from muliao.sessions.stopping import StoppableRuns, Stopped
from muliao.sessions.tables import Mail, Message

log = logging.getLogger(__name__)


def resumable(history: list[ModelMessage], delegated: Delegated) -> bool:
    """历史从中途续跑：停在一条没得到回复、也没被停止的请求上，或最后的回复里有没出结果的工具调用（挂起的调用除外，
    见 pending.pending_calls；这些调用一般不重新执行，由 Consumer._run 补上「被中断」的结果后重发）。

    停在挂起的调用上时不是续跑，哪怕末尾是一条请求：挂起的调用与别的工具在同一个回复里时，pydantic-ai 以挂起结束时
    把别的工具的结果追加成一条请求，这时要等回答（或跳过）作为挂起调用的结果交回，不能把那条请求重发出去。"""
    pending = pending_calls(history, delegated)
    if not history or pending:
        return False
    last = history[-1]
    if isinstance(last, ModelRequest):
        return not is_stopped(last)
    return bool(unanswered_calls(history))


def interrupted_calls(history: list[ModelMessage], delegated: Delegated) -> list[ToolCallPart]:
    """这次运行要补上「被中断」结果的调用：历史停在一条回复上（这一步在执行工具时被打断——进程退出，或这一步
    失败），它里面没出结果、又不在等回信的调用。挂起的调用不在其中：它们的结果由 sessions.pending 交回。

    可以原样重放的不补：没出结果的调用全是还没交出去的 ask_agent 时重新执行按调用 id 幂等（sessions.holds，
    没有占用就是没投出去），没有别的副作用。只要有一个不能重放，整步都补结果、都不重放——pydantic-ai 续跑时要么
    执行这条回复里所有没结果的调用、要么一个都不执行，挑不出单独一个来（停在挂起的调用上时把已有结果的调用标为
    跳过，所以补过结果的不会再执行）。"""
    if not history or not isinstance(history[-1], ModelResponse):
        return []
    pending = pending_calls(history, delegated)
    calls = [c for c in unanswered_calls(history) if c.tool_call_id not in pending]
    return [] if all(replayable(c.tool_name) for c in calls) else calls


def paused_end(calls: Sequence[ToolCallPart]) -> RunEnd:
    """停在这些挂起的调用上：ask_user 的问题要告诉用户。"""
    questions = [q for c in calls for q in questions_in(c)]
    return RunEnd(
        answer=waiting_text(questions) if questions else "",
        paused=True,
        asked_user=bool(questions),
        stopped=False,
    )


def end_of(outcome: Finished | Paused) -> RunEnd:
    """模型运行的结局怎么收尾：最终回答，或停在挂起的调用上。"""
    if isinstance(outcome, Finished):
        return RunEnd(answer=outcome.answer, paused=False, asked_user=False, stopped=False)
    if any(c.tool_name not in DEFERRED_TOOLS for c in outcome.calls):
        # 只有 ask_user / ask_agent 会挂起；其他挂起说明工具与这里的约定不一致
        raise RuntimeError(f"run deferred unexpected tool calls: {[c.tool_name for c in outcome.calls]}")
    return paused_end(outcome.calls)


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
        runs: StoppableRuns[Commit],
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
        # 正在进行的运行的登记：停止会话的命令（sessions.halt.SessionStopper）经同一份登记停下运行
        self._runs = runs
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
        # 上下文超过阈值时先压缩（之后每一步之前同样检查，见 sessions.steps.CompactionDue）
        async with self._db.transaction() as s:
            context = await load_context(s, session.id)
        verdict = self._compactor.verdict(session.id, context.messages, context.stale_responses)
        compacted = await self._compactor.compact(session, agent) if verdict == Verdict.DUE else None
        async with self._db.transaction() as s:
            if compacted is not None:
                context = await load_context(s, session.id)
            queued = await queued_in(s, session.id)
            # 都带上会话条件：messages.task_id 没有索引，按会话的索引只扫这个会话的行
            earlier = list(
                (
                    await s.scalars(
                        select(Mail).where(Mail.session_id == session_id, Mail.consumed_by == run_id)
                    )
                ).all()
            )
            delegated = await called_in(s, session.id)
            adopted = await _adopted(s, session.id, run_id) if resumable(context.messages, delegated) else []
            saved_rows = list(
                (
                    await s.scalars(
                        select(Message)
                        .where(Message.session_id == session_id, Message.task_id == run_id)
                        .order_by(Message.id)
                    )
                ).all()
            )
            last_message = await s.scalar(
                select(func.max(Message.id)).where(Message.session_id == session_id)
            )
        history = context.messages
        state = RunState(
            run_id=run_id,
            session=session,
            agent=agent,
            # 补完被中断的调用之后才定下（补出来的那条请求就是要重发的）
            resending=False,
            handled=[*earlier, *adopted],
            messages=load_messages(r.payload for r in saved_rows),
            last_row=saved_rows[-1] if saved_rows else None,
        )
        state.stale_responses = context.stale_responses
        state.compaction_exhausted = verdict == Verdict.HOPELESS or compacted == 0
        self._active[session_id] = state
        # 被打断的那一步里没出结果的调用不重新执行——副作用可能已经发生，而我们无从知道，重放一条重启服务的命令
        # 会让恢复无限循环。补上「被中断、没有重新执行」的结果交给模型判断：没有挂起的调用时这次运行就从这条补
        # 出来的请求重发（下面的 resending），停在挂起的调用上时它让那些调用在交回回答时被标为跳过
        if interrupted := interrupted_calls(history, delegated):
            log.warning(
                "resuming an interrupted step; its unfinished tool calls are not re-executed",
                extra={
                    "session_id": session_id,
                    "run_id": run_id,
                    "tools": [c.tool_name for c in interrupted],
                },
            )
            filled = interrupted_results(interrupted)
            last_message = await self._steps.save_interrupted(state, filled)
            history = [*history, *filled]
        state.resending = resumable(history, delegated) and isinstance(history[-1], ModelRequest)
        prompt, deferred = await self._opening(state, history, queued, delegated)
        if prompt is None and deferred is None and not resumable(history, delegated):
            # 没有要交给模型的：信箱空了，或停在挂起的调用上、还不能继续。之前的尝试已经跑完、只差收尾时（它保存的就是
            # 历史的末尾），按保存的历史收尾；之后的运行已经接着它保存的部分继续了（重试一个过时的失败运行），推送与
            # 委派的结局由那些运行做过，这里只了结它自己。历史停在标着停止的地方时，会话在这次运行登记可停止之前、或在它
            # 最终失败之后、被手动重试之前被停止了：结局由停止做过（sessions.halt），也只了结它自己
            if state.last_row is None or state.last_row.id != last_message or is_stopped(history[-1]):
                state.messages, state.handled = [], []
            log.info("nothing to consume", extra={"session_id": session_id, "run_id": run_id})
            return (
                self._finisher.commit(state, settled_end(history, delegated))
                if state.messages
                else self._finisher.idle(state)
            )
        # 运行开始时就定下（工具与指令随之确定）：会话种类决定回答交给谁（KindPolicy.audience）；用户是否在等看它处理
        # 过的、和这次要处理的信件
        awaited = any(m.awaited for m in [*state.handled, *queued])
        audience = policy_of(session.kind).audience(awaited)
        scope = self._runtime.scope(agent, session_id=session.id, audience=audience)
        # 随运行变化的上下文（当前时间、回答交给谁）不进 instructions——那是缓存前缀——而是挂在这次运行新增的
        # 第一条请求上（sessions.steps._before_request）
        state.run_context = self._runtime.run_context(audience)
        profile = self._tool_groups.profile(agent.id, agent.tool_groups)
        self._live.begin(session_id, last_message)
        while True:
            try:
                outcome = await self._runtime.run_steps(
                    agent,
                    scope,
                    prompt,
                    history,
                    profile,
                    self._steps.hooks(state),
                    origin_note=origin_note(session.origin),
                    summary=context.summary,
                    deferred=deferred,
                )
                break
            except CompactionDue:
                # 这次请求已经保存：压缩，再从库里加载上下文（摘要 + 切点之后），从它续跑
                compacted = await self._compactor.compact(session, agent)
                async with self._db.transaction() as s:
                    context = await load_context(s, session.id)
                    compaction = await latest_compaction(s, session.id)
                if (
                    compaction is not None
                    and state.run_context_row is not None
                    and state.run_context_row <= compaction.through_message_id
                ):
                    # 挂着运行上下文的那条请求被压进了摘要，模型不再看得到它：下一条新请求重新挂一段（时间取现在）
                    state.run_context = self._runtime.run_context(audience)
                    state.run_context_row = None
                history, prompt, deferred = context.messages, None, None
                state.stale_responses = context.stale_responses
                state.compaction_exhausted = compacted == 0
                state.resending = True
        await self._steps.save_tail(state)
        return self._finisher.commit(state, end_of(outcome))

    async def _opening(
        self, state: RunState, history: list[ModelMessage], queued: list[Mail], delegated: Delegated
    ) -> tuple[list[UserContent] | None, dict[str, object] | None]:
        """这次运行开头交给模型的：新信件的内容，以及挂起调用的结果（`{调用 id: 结果}`）。从历史续跑、或还在等挂起调用的回答时什么都不交。"""
        if resumable(history, delegated) or waiting(history, queued, delegated):
            return None, None
        pending = pending_calls(history, delegated)
        answers = answered_in(queued, pending)
        deferred = deferred_results(pending, answers) if pending else None
        state.taken = list(queued)
        if queued:
            # 开头取走的信件都交给了这次运行（mailbox.handed_to），哪怕它在第一次保存之前就最终失败：失败的结清据此
            # 了结它们，也不会为它们再唤醒一次运行
            async with self._db.transaction() as s:
                await hand_over_in(s, state.run_id, queued[-1].id)
        content = await self._content.of(state.agent.id, [m for m in queued if not is_on_time(m, pending)])
        # 开头的内容由 pydantic-ai 放进第一次请求的用户消息（下标从 0 起），事件在那次请求保存前标上
        state.opening = content
        return (content.items or None), deferred

    # ---- 停止（命令见 sessions.halt.SessionStopper）----

    async def _stopped(self, run_id: int, session_id: str) -> Commit:
        state = self._active.get(session_id)
        saved = 0 if state is None else await self._steps.save_stopped(state)
        log.info("run stopped by user", extra={"session_id": session_id, "run_id": run_id, "saved": saved})
        if await self._halt(session_id) == Halted.GONE:
            # 正在运行的会话忙，不能被删除：走到这里是 bug
            raise RuntimeError(f"session {session_id} was deleted while its run was being stopped")
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
    return list(
        (
            await s.scalars(select(Mail).where(Mail.session_id == session_id, Mail.consumed_by == tail_run))
        ).all()
    )


class RunHandler:
    """队列里的运行（run task）。"""

    def __init__(self, consumer: Consumer, queue: TaskQueue, messaging: Messaging):
        self._consumer = consumer
        self._queue = queue
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
        退出信箱，不会在之后的运行里被悄悄执行。运行开始之后才到的不动：同一个事务里立即唤醒新的运行处理它们（如对
        同一 thread 的第二项委派），不等信箱对账。队列已把这次运行记为 failed，唤醒不会把它当成忙。

        同一事务发布 RunFailed（如 issue 看板据此在时间线上记一笔、通知用户）。这个钩子自己失败时（worker 不带钩子记下
        失败）不发：失败仍在任务页与日志里可见。"""
        if task.session_id is None or task.error is None:
            raise RuntimeError(f"run {task.id} has no session or no recorded error")
        child, agent = await load_session(s, task.session_id)
        await settle_failure_in(s, self._queue, child, task)
        await wake_in(s, self._queue, child)
        self._messaging.publish_in(
            s,
            RunFailed(
                session_id=child.id,
                agent_id=agent.id,
                run_id=task.id,
                origin=child.origin,
                origin_ref=child.origin_ref,
                error=task.error,
            ),
        )
