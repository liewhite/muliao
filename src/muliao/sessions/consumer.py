"""会话的消费者：把信箱里的信件交给模型。设计见 docs/architecture.md「会话信箱」。

一次运行（一条 run task）的编排，各部分在各自的模块里：
- 起点由会话停在哪决定（sessions.pending.position，唯一的判断）：从历史续跑（Resend）就重发末尾那条请求、或重新
  执行可以安全重放的调用；被打断（Interrupted）就先补上「被中断、没有重新执行」的结果
  （sessions.history.interrupted_results）再看补完之后停在哪；等回答（Paused）且能继续时把回答作为挂起调用的结果交回；
  新开（Fresh）时把信箱里排队的信件作为开头交给模型（sessions.content）。
- 逐步运行、每一步保存（sessions.steps）；上下文超过阈值时在步与步之间压缩后续跑（sessions.compaction）。
- 收尾（sessions.finish），与运行记为完成在同一个事务里。
- 用户停止（sessions.halt）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, UserContent
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import AgentRuntime, Finished, Paused, Toolbox
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, TaskQueue, TaskView
from muliao.sessions import pending
from muliao.sessions.approvals import take_in as take_approvals_in
from muliao.sessions.approvals import waiting_text as approval_waiting_text
from muliao.sessions.codec import load_messages
from muliao.sessions.content import Content, ContentBuilder
from muliao.sessions.conventions import answer_text, is_stopped
from muliao.sessions.events import RunFailed
from muliao.sessions.finish import RunEnd, RunFinisher
from muliao.sessions.halt import Halted, SessionHalt
from muliao.sessions.history import (
    interrupted_results,
    last_message_id_in,
    last_saved_by_in,
    load_context,
    saved_by_in,
)
from muliao.sessions.live import LiveSegments
from muliao.sessions.loading import load_session
from muliao.sessions.mailbox import audience_of, consumed_by_in, delegated_in, queued_in, wake_in
from muliao.sessions.origins import OriginNotes
from muliao.sessions.questions import waiting_text
from muliao.sessions.runs import RunPayload, hand_over_in
from muliao.sessions.settlement import settle_failure_in
from muliao.sessions.steps import CompactionDue, RunState, Steps
from muliao.sessions.stopping import StoppableRuns
from muliao.sessions.tables import Mail

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Opening:
    """一次运行开头要交给模型的（Consumer._opening，不改库）：取走的信件、停在挂起的调用上时那些调用与对它们的回答、
    其余信件的内容。"""

    queued: list[Mail]
    paused: pending.Paused | None
    content: Content


@dataclass
class _Current:
    """一次运行读出的状态（Consumer.run 里这次运行自己持有）：被停止时从这里取（运行的协程已被取消），还没读出时为 None。"""

    state: RunState | None = None


def paused_end(
    calls: Sequence[ToolCallPart], gated: Sequence[ToolCallPart], requested: Mapping[str, str]
) -> RunEnd:
    """停在这些挂起的调用上（calls 等回信，gated 等批准，requested 是其中这次新请求的及其 pin）：ask_user 的问题、等批准的
    调用要告诉用户。"""
    questions = [q for c in calls for q in pending.questions_in(c)]
    texts = [waiting_text(questions)] if questions else []
    if gated:
        texts.append(approval_waiting_text([_brief(c) for c in gated]))
    return RunEnd(
        answer="\n".join(texts),
        paused=True,
        asked_user=bool(questions),
        stopped=False,
        approvals=tuple(c.tool_call_id for c in gated),
        requested=requested,
    )


def _brief(call: ToolCallPart) -> str:
    """通知里等批准的调用：工具名与参数（长的截短，全文在会话里）。"""
    args = call.args_as_json_str()
    return f"{call.tool_name} {args if len(args) <= 300 else args[:300] + '…'}"


def end_of(outcome: Finished | Paused) -> RunEnd:
    """模型运行的结局怎么收尾：最终回答，或停在挂起的调用上。"""
    if isinstance(outcome, Finished):
        return RunEnd(answer=outcome.answer, paused=False, asked_user=False, stopped=False)
    if any(c.tool_name not in pending.DEFERRED_TOOLS for c in outcome.calls):
        # 只有 ask_user / ask_agent 会挂起；其他挂起说明工具与这里的约定不一致
        raise RuntimeError(f"run deferred unexpected tool calls: {[c.tool_name for c in outcome.calls]}")
    if any(c.tool_name in pending.DEFERRED_TOOLS for c in outcome.approvals):
        # 挂起的工具按工具名认，等批准的按记录认（sessions.approvals）：同一个工具两样都做会认不清
        raise RuntimeError(f"a deferred tool asked for approval: {[c.tool_name for c in outcome.approvals]}")
    return paused_end(outcome.calls, outcome.approvals, outcome.pins)


def settled_end(history: list[ModelMessage], at: pending.Position) -> RunEnd:
    """之前的尝试已经跑完、只差收尾：从保存的历史与它停在哪（at）得出它怎么结束的。"""
    if (waiting := pending.paused(at)) is not None:
        calls = list(waiting.pending.values())
        return paused_end(
            [p.call for p in calls if p.kind is not pending.AWAITING_APPROVAL],
            [p.call for p in calls if p.kind is pending.AWAITING_APPROVAL],
            # 等批准的正是按 approval_requests 的记录认出来的：之前的尝试结束时已记下（Steps.save_end，连同 pin），没有新请求
            {},
        )
    last = history[-1]
    if not isinstance(last, ModelResponse):
        raise RuntimeError(f"a settled run must end with a model response, history ends with {last!r}")
    return RunEnd(answer=answer_text(last), paused=False, asked_user=False, stopped=False)


class Consumer:
    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        runtime: AgentRuntime,
        toolbox: Toolbox,
        live: LiveSegments,
        content: ContentBuilder,
        steps: Steps,
        finisher: RunFinisher,
        runs: StoppableRuns[Commit],
        halt: SessionHalt,
        notes: OriginNotes,
    ):
        self._db = db
        self._queue = queue
        self._runtime = runtime
        self._toolbox = toolbox
        self._live = live
        self._content = content
        self._steps = steps
        self._finisher = finisher
        self._halt = halt
        self._notes = notes
        # 正在进行的运行的登记：停止会话的命令（sessions.halt.SessionStopper）经同一份登记停下运行
        self._runs = runs

    async def run(self, run_id: int, session_id: str) -> Commit:
        """执行一次运行直到模型不再调用工具（或停在挂起的调用上、被用户停止），返回它的收尾。"""
        current = _Current()
        try:
            return await self._runs.run(
                session_id,
                lambda: self._run(run_id, session_id, current),
                lambda: self._stopped(run_id, session_id, current),
            )
        finally:
            self._live.end(session_id)

    async def _run(self, run_id: int, session_id: str, current: _Current) -> Commit:
        async with self._db.transaction() as s:
            session, agent = await load_session(s, session_id)
            context = await load_context(s, session.id)
            queued = await queued_in(s, session.id)
            delegated_work = await delegated_in(s, session.id)
            earlier = await consumed_by_in(s, session_id, run_id)
            at = pending.position(context.messages, await pending.handed_out_in(s, session.id), queued)
            resuming = isinstance(at.after if isinstance(at, pending.Interrupted) else at, pending.Resend)
            adopted = await _adopted(s, session.id, run_id) if resuming else []
            saved_rows = await saved_by_in(s, session_id, run_id)
            state = RunState(
                run_id=run_id,
                session=session,
                agent=agent,
                context=context,
                handled=[*earlier, *adopted],
                messages=load_messages(r.payload for r in saved_rows),
                last_row=saved_rows[-1] if saved_rows else None,
            )
            # 被打断的那一步里没出结果的调用不重新执行——副作用可能已经发生，而我们无从知道，重放一条重启服务的命令
            # 会让恢复无限循环。补上「被中断、没有重新执行」的结果交给模型判断，在读历史的同一个事务里存库（也接进
            # state.context）：交给 pydantic-ai 的就是库里的历史，没有只在内存里补过的部分。补完之后停在 at.after
            if isinstance(at, pending.Interrupted):
                log.warning(
                    "resuming an interrupted step; its unfinished tool calls are not re-executed",
                    extra={
                        "session_id": session_id,
                        "run_id": run_id,
                        "tools": [c.tool_name for c in at.calls],
                    },
                )
                await self._steps.save_in(s, state, interrupted_results(at.calls), consumed=[])
                at = at.after
            last_message = await last_message_id_in(s, session_id)
        current.state = state
        history = state.context.messages
        opening = await self._opening(state, at, queued)
        if opening is None and not isinstance(at, pending.Resend):
            # 没有要交给模型的：信箱空了，或停在挂起的调用上、还不能继续。之前的尝试已经跑完、只差收尾时（它保存的就是
            # 历史的末尾），按保存的历史收尾；之后的运行已经接着它保存的部分继续了（重试一个过时的失败运行），推送与
            # 委派的结局由那些运行做过，这里只了结它自己。历史停在标着停止的地方时，会话在这次运行登记可停止之前、或在它
            # 最终失败之后、被手动重试之前被停止了：结局由停止做过（sessions.halt），也只了结它自己
            settled = (
                state.last_row is not None
                and state.last_row.id == last_message
                and not is_stopped(history[-1])
            )
            log.info("nothing to consume", extra={"session_id": session_id, "run_id": run_id})
            return (
                self._finisher.commit(state, settled_end(history, at))
                if settled
                else self._finisher.idle(state)
            )
        # 这次运行的配置快照：模型、思考档位与开放的工具在运行开始时定下，整次运行（含中途压缩后续跑、压缩用的模型）
        # 都用这一份，运行中途改 agent.toml 或权限从下一次运行起生效，也不会让进行中的运行半路失败
        config = await self._runtime.config(agent.id)
        profile = await self._toolbox.profile(agent)
        # 运行开始时就定下（工具与指令随之确定）：回答交给谁看它处理过的、和这次要处理的信件，以及会话有没有委派来、
        # 还没结清的工作（mailbox.audience_of）
        audience = audience_of([*state.handled, *queued], delegated_work)
        scope = self._runtime.scope(agent, session_id=session.id, audience=audience)

        # 随运行变化的上下文（当前时间、回答交给谁）不进 instructions——那是缓存前缀——而是挂在这次运行新增的
        # 第一条请求上（sessions.steps._before_request），挂的时候才生成，时间取当时
        def run_context() -> str:
            return self._runtime.run_context(audience)

        # 上下文超过阈值时先压缩（之后每次请求保存后同样检查，见 sessions.steps.CompactionDue），和运行中途走同一个
        # Steps.compaction_due 与 Steps.compact。放在确认有要交给模型的之后（没事可做的运行不花一次总结），交出信件与批准
        # 之前（压缩失败重试时，批准不会被当成已经交过、不再执行）
        if self._steps.compaction_due(state):
            await self._steps.compact(state, config)
        prompt, deferred = (None, None) if opening is None else await self._hand_over(state, opening)
        self._live.begin(session_id, last_message)
        while True:
            try:
                outcome = await self._runtime.run_steps(
                    config,
                    scope,
                    prompt,
                    state.context.messages,
                    profile,
                    self._steps.hooks(state, run_context),
                    origin_note=self._notes[session.origin],
                    summary=state.context.summary,
                    deferred=deferred,
                )
                break
            except CompactionDue:
                # 这次请求已经保存：压缩，再从库里加载上下文（摘要 + 切点之后），从它续跑：历史停在那条请求上，
                # 续跑就是重发它（agent_runtime 的续跑语义）
                await self._steps.compact(state, config)
                prompt, deferred = None, None
        end = end_of(outcome)
        # 停在挂起的调用上时别的工具的结果还没保存：和等批准的记录同一个事务，且在运行还登记可停止时做——之后才到的停止
        # 直接从库里补齐历史，库里得已经是这次运行的完整结尾
        await self._steps.save_end(state, end.requested)
        return self._finisher.commit(state, end)

    async def _opening(self, state: RunState, at: pending.Position, queued: list[Mail]) -> Opening | None:
        """这次运行开头要交给模型的：新信件的内容，以及挂起调用的回答。从历史续跑、还在等挂起调用的回答、空闲、或信件里
        没有内容时为 None。不改库（附件复制进工作目录是可重做的派生副本），交出去由 _hand_over 做。"""
        match at:
            case pending.Paused(ready=True, answer_mail=answer_mail):
                # 回答作为挂起调用的结果交回，其余信件（事件、新消息、迟到的回答）作为内容
                answering = {m.id for m in answer_mail}
                rest = [m for m in queued if m.id not in answering]
                return Opening(queued, at, await self._content.of(state.agent.id, rest))
            case pending.Fresh():
                content = await self._content.of(state.agent.id, queued)
                return Opening(queued, None, content) if content.items else None
            case _:
                return None

    async def _hand_over(
        self, state: RunState, opening: Opening
    ) -> tuple[list[UserContent] | None, dict[str, object] | None]:
        """把开头的信件交给这次运行：返回交给模型的内容，以及挂起调用的结果（`{调用 id: 结果}`）。"""
        interrupted: dict[str, object] = {}
        answers = {} if opening.paused is None else opening.paused.answers
        if opening.queued:
            # 开头取走的信件都交给了这次运行（mailbox.handed_to），哪怕它在第一次保存之前就最终失败：失败的结清据此
            # 了结它们，也不会为它们再唤醒一次运行。批准在同一个事务里记为已交给运行执行：之前交过的不再执行
            async with self._db.transaction() as s:
                await hand_over_in(s, self._queue, state.run_id, opening.queued[-1].id)
                interrupted = await take_approvals_in(s, state.session.id, answers)
        deferred = (
            None
            if opening.paused is None
            else pending.deferred_results(opening.paused.pending, answers, replaced=interrupted)
        )
        self._steps.begin(state, opening.queued, opening.content)
        return (opening.content.items or None), deferred

    # ---- 停止（命令见 sessions.halt.SessionStopper）----

    async def _stopped(self, run_id: int, session_id: str, current: _Current) -> Commit:
        state = current.state
        log.info("run stopped by user", extra={"session_id": session_id, "run_id": run_id})
        save_handed = None if state is None else partial(self._steps.save_handed_in, state=state)
        if await self._halt(session_id, first=save_handed) == Halted.GONE:
            # 正在运行的会话忙，不能被删除：走到这里是 bug
            raise RuntimeError(f"session {session_id} was deleted while its run was being stopped")
        if state is None:
            # 停在这次运行读出状态之前：它什么都没做。停止之后、它记为完成之前到的信没能唤醒运行（它还占着），
            # 由这里唤醒（停止后马上投信，如 issue_stop 的收尾消息）
            return self._finisher.wake(session_id)
        return self._finisher.commit(state, RunEnd("", paused=False, asked_user=False, stopped=True))


async def _adopted(s: AsyncSession, session_id: str, run_id: int) -> list[Mail]:
    """从历史中途续跑、而停下的那部分是另一次运行保存的（它失败了，之后有新信件唤醒了这次运行）：这次运行接着做完
    它的工作，也接管它处理过的信件——推送由这次运行做。之后再重试那次运行时什么都不做（Consumer._run 的「没有要
    交给模型的」）。"""
    tail_run = await last_saved_by_in(s, session_id)
    if tail_run is None or tail_run == run_id:
        return []
    return await consumed_by_in(s, session_id, tail_run)


class RunHandler:
    """队列里的运行（run task）。"""

    # mail.consumed_by 指向运行（ON DELETE CASCADE）：删了会连带删掉它处理过的信件与幂等键
    prunable = False

    def __init__(self, consumer: Consumer, queue: TaskQueue, messaging: Messaging):
        self._consumer = consumer
        self._queue = queue
        self._messaging = messaging

    async def execute(self, task: Task) -> Commit:
        if task.session_id is None:
            raise RuntimeError(f"run {task.id} has no session")
        RunPayload.of(TaskView.of(task))  # 在第一现场校验存下的 payload，不等到唤醒或失败收尾时才读到它
        return await self._consumer.run(task.id, task.session_id)

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """最终失败：交给这个会话、没能做完的委派以「失败」结清（发起方不会一直等）。按会话范围：已交给模型、还没结清的
        全部（包括早先停在挂起调用上的运行处理过的、被续跑接管的），加上交给过这次运行、还排着的——运行在第一次保存
        之前就失败时，开头取走的信件一直排着，也不会再有运行因为它们被唤醒（mailbox.handed_to）；它们随结清一起
        退出信箱，不会在之后的运行里被悄悄执行。运行开始之后才到的不动：同一个事务里立即唤醒新的运行处理它们（如对
        同一 thread 的第二项委派），不等信箱对账。队列已把这次运行记为 failed，唤醒不会把它当成忙。

        同一事务发布 RunFailed（通知领域据此通知用户，issue 看板在时间线上记一笔）。这个钩子自己失败时（worker 不带钩子记下
        失败）不发：失败仍在任务页与日志里可见。"""
        if task.session_id is None or task.error is None:
            raise RuntimeError(f"run {task.id} has no session or no recorded error")
        child, agent = await load_session(s, task.session_id)
        await settle_failure_in(s, self._queue, child, TaskView.of(task))
        await wake_in(s, self._queue, child)
        self._messaging.publish_in(
            s,
            RunFailed(
                session_id=child.id,
                agent_id=agent.id,
                agent_name=agent.name,
                run_id=task.id,
                origin=child.origin,
                origin_ref=child.origin_ref,
                error=task.error,
            ),
        )
