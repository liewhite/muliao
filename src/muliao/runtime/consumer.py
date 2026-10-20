"""会话的消费者：把信箱里的信件交给模型，逐步运行、每一步保存。设计见 docs/architecture.md「会话信箱」。

一次运行（一条 turn task）：
- 起点由历史决定：历史停在一条没得到回复的请求上就重发它；最后一条回复里有没执行完的工具调用（进程在执行
  工具时退出）就执行它们；停在提问上时，回答作为那次调用的结果交回（没回答的记为跳过或没回答）；否则把信箱里
  排队的信件作为开头交给模型。
- 每一步保存（StepHooks）：请求模型之前把新到的信件并进这条请求，请求与它带上的信件（记为已处理）同一个事务提交；
  模型回复之后提交这条回复。失败或重启后从上一步续跑，已经保存的步骤不会重放（只有中断那一步里正在执行的工具
  会再执行一遍）。
- 结束时的收尾（finish_in，与运行记为完成在同一个事务里）：推送、交回委派的结果、反思计数、压缩判断、唤醒
  下一次运行。
- 用户停止：保存停止前已经产生、还没保存的部分，没出结果的调用补上「被停止」的结果；清空信箱；结束这个会话的
  后台任务与它 report_back 委派出去、还在跑的 thread。

两个调用方：队列里的运行（RunHandler），以及 ask_agent 在发问方那一步里内联跑完私有 thread 的一次运行（run_inline）。
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace

from pydantic_ai import DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.bus import EventBus, LiveRuns, task_event
from muliao.conventions import (
    STOPPED_TOOL_RESULT,
    TURN_EVENTS,
    TURN_STOPPED,
    event_indices,
    format_event,
    unanswered_calls,
)
from muliao.db import Database, utcnow
from muliao.delivery.base import Notifier
from muliao.files import FileStore
from muliao.models import (
    Agent,
    Mail,
    MailKind,
    MailState,
    Message,
    Notification,
    ReplyTo,
    Session,
    Task,
    TaskKind,
    TaskStatus,
)
from muliao.runtime.answers import Answer
from muliao.runtime.attachments import copy_to_workspace, references
from muliao.runtime.codec import dump_message, load_messages
from muliao.runtime.compaction import CompactionScheduler, load_context
from muliao.runtime.conversation import failure_of
from muliao.runtime.mailbox import consume_in, discard_in, handed_to, latest_run, post_in, queued_in, wake_in
from muliao.runtime.questions import (
    ASK_USER,
    AskResult,
    deferred_results,
    late_answer_text,
    pending_questions,
    questions_of,
    waiting_text,
)
from muliao.runtime.redaction import redact_messages
from muliao.runtime.reports import failed_text, finished_text, open_reports, settle_in
from muliao.runtime.runner import AgentRuntime, StepHooks, ToolGroups
from muliao.runtime.stopping import StoppableRuns, Stopped
from muliao.runtime.tasks import Commit, TaskQueue, no_commit, reflection_lane, turn_lane

log = logging.getLogger(__name__)


@dataclass
class _Run:
    """一次运行在进行中的状态（只在这次运行的协程里用）。"""

    run_id: int
    session: Session
    agent: Agent
    # 从历史末尾那条没得到回复的请求续跑：第一次请求是重发它，它已经在库里
    resending: bool
    # 运行中的历史里已经保存的条数（started 时按 pydantic-ai 规整后的历史定基准）
    saved: int = 0
    # 运行中的历史本身（pydantic-ai 的 ctx.state.message_history，第一次回调时拿到）：被停止时从它取回没保存的部分
    live: list[ModelMessage] | None = None
    # 交给模型、等这一步保存时记为已处理的信件
    taken: list[Mail] = field(default_factory=list)
    # 开头交给模型的内容里哪些段是事件，第一次请求保存前标上（_mark_events）
    opening_events: list[int] = field(default_factory=list)
    # 这次运行处理过的信件：包括这次运行之前几次尝试里已经保存的（重试沿用同一个运行），以及接着别的运行保存的
    # 部分续跑时那次运行处理过的（_adopted）
    handled: list[Mail] = field(default_factory=list)
    # 这次运行保存的消息（包括之前几次尝试保存的），和最后保存的那一行（停止时补标记）
    messages: list[ModelMessage] = field(default_factory=list)
    last_row: Message | None = None


@dataclass(frozen=True)
class RunOutcome:
    answer: str
    messages: list[ModelMessage]
    stopped: bool
    # 收尾：与运行记为完成在同一个事务里执行
    finish: Commit


async def _load(s: AsyncSession, session_id: str) -> tuple[Session, Agent]:
    session = await s.get(Session, session_id)
    if session is None:
        raise RuntimeError(f"session {session_id} vanished during its run")
    agent = await s.get(Agent, session.agent_id)
    if agent is None:
        raise RuntimeError(f"session {session_id} references missing agent {session.agent_id}")
    return session, agent


def _settled_answer(history: list[ModelMessage]) -> str:
    """已经跑完的运行的回答，从保存的历史取：停在提问上时是问题列表，否则是最后一条回复的文字。"""
    if pending := pending_questions(history):
        return waiting_text([q for questions in pending.values() for q in questions])
    last = history[-1]
    if not isinstance(last, ModelResponse):
        raise RuntimeError(f"a settled run must end with a model response, history ends with {last!r}")
    return last.text or ""


def _answer_text(output: str | DeferredToolRequests) -> str:
    """一次运行的回答；停在提问上时是问题列表（用于通知与 ask_agent 之外的展示）。"""
    if isinstance(output, str):
        return output
    if output.approvals or any(c.tool_name != ASK_USER for c in output.calls):
        # 只有 ask_user 会挂起；其他挂起说明工具与这里的约定不一致
        raise RuntimeError(
            f"run deferred unexpected tool calls: calls={[c.tool_name for c in output.calls]} "
            f"approvals={[c.tool_name for c in output.approvals]}"
        )
    return waiting_text([q for c in output.calls for q in questions_of(c)])


class Consumer:
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
        stop_jobs: Callable[[str], Awaitable[None]],
    ):
        self._db = db
        self._runtime = runtime
        self._tool_groups = tool_groups
        self._queue = queue
        self._notifier = notifier
        self._compaction = compaction
        self._files = files
        self._live = live
        # 内联运行不经 worker，自己发 task 事件（前端靠它在一次运行结束后重新拉取）
        self._bus = bus
        self._stop_jobs = stop_jobs
        self._runs: StoppableRuns[RunOutcome] = StoppableRuns()
        # 正在进行的运行的状态，按会话：被停止时 _stopped 从这里取（运行的协程已被取消）
        self._active: dict[str, _Run] = {}
        # 私有 thread 的内联运行：同一 thread 的并行 ask 在这里串行（进程内，单进程部署）
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    # ---- 一次运行 ----

    async def run(self, run_id: int, session_id: str, *, inline: bool = False) -> RunOutcome:
        """执行一次运行直到模型不再调用工具（或停在提问上、被用户停止）。用户停止时返回 stopped 的结果。"""
        try:
            result = await self._runs.run(session_id, lambda: self._run(run_id, session_id, inline=inline))
            if isinstance(result, Stopped):
                return await self._stopped(run_id, session_id)
            return result
        finally:
            self._active.pop(session_id, None)
            self._live.end(session_id)

    async def _run(self, run_id: int, session_id: str, *, inline: bool) -> RunOutcome:
        async with self._db.transaction() as s:
            session, agent = await _load(s, session_id)
            context = await load_context(s, session.id)
            queued = await queued_in(s, session.id)
            earlier = list((await s.scalars(select(Mail).where(Mail.consumed_by == run_id))).all())
            adopted = await _adopted(s, session.id, run_id) if self._resumable(context.messages) else []
            saved_rows = list(
                (await s.scalars(select(Message).where(Message.task_id == run_id).order_by(Message.id))).all()
            )
            last_message = await s.scalar(
                select(func.max(Message.id)).where(Message.session_id == session_id)
            )
        history = context.messages
        state = _Run(
            run_id=run_id,
            session=session,
            agent=agent,
            resending=self._resumable(history) and isinstance(history[-1], ModelRequest),
            handled=[*earlier, *adopted],
            messages=load_messages(r.payload for r in saved_rows),
            last_row=saved_rows[-1] if saved_rows else None,
        )
        # 被停止时 _stopped 从这里取状态（运行的协程已被取消）；run 结束时统一清掉
        self._active[session_id] = state
        prompt, deferred = await self._opening(state, history, queued)
        # 运行开始时就定下（工具与指令随之确定）：它处理过的、和这次要处理的信件里有用户在等的
        awaited = any(m.awaited for m in [*state.handled, *queued])
        reply_to = ReplyTo.AGENT if inline else ReplyTo.USER if awaited else ReplyTo.NONE
        scope = self._runtime.scope(agent, session_id=session.id, origin=session.origin, reply_to=reply_to)
        if prompt is None and deferred is None and not self._resumable(history):
            # 没有要交给模型的：信箱空了，或停在提问上、只有事件在等。之前的尝试已经跑完、只差收尾时（它保存的就是
            # 历史的末尾），用保存的回答收尾；之后的运行已经接着它保存的部分继续了（重试一个过时的失败运行），
            # 回答与推送由那些运行给过，这里只了结它自己
            if state.last_row is None or state.last_row.id != last_message:
                state.messages, state.handled = [], []
            answer = _settled_answer(history) if state.messages else ""
            log.info("nothing to consume", extra={"session_id": session_id, "run_id": run_id})
            return RunOutcome(
                answer, state.messages, False, self._finisher(state, answer, stopped=False, inline=inline)
            )
        self._live.begin(session_id, last_message)
        result = await self._runtime.run_steps(
            agent,
            scope,
            prompt,
            history,
            self._tool_groups.profile(agent.id, agent.tool_groups),
            StepHooks(
                started=lambda messages: self._started(state, messages),
                before_request=lambda request, messages: self._before_request(state, request, messages),
                after_response=lambda response, messages: self._after_response(state, response, messages),
            ),
            summary=context.summary,
            deferred=deferred,
        )
        # 停在提问上时，挂起之外已经执行完的工具结果在最后一条请求里，一起保存
        await self._save_tail(state)
        answer = _answer_text(result.output)
        return RunOutcome(
            answer, state.messages, False, self._finisher(state, answer, stopped=False, inline=inline)
        )

    async def _opening(
        self, state: _Run, history: list[ModelMessage], queued: list[Mail]
    ) -> tuple[list[UserContent] | None, DeferredToolResults | None]:
        """这次运行开头交给模型的：新消息（信件内容），以及挂起提问的结果。从历史续跑时什么都不交（信件留到下一次请求）。"""
        if self._resumable(history):
            return None, None
        pending = pending_questions(history)
        if pending and all(m.kind == MailKind.EVENT for m in queued):
            # 停在提问上：只有回答或新消息（跳过）能继续，事件等它们一起交给模型（与 mailbox.wake_in 同一条规则；
            # 走到这里是重试了一次已经停在提问上的运行）
            return None, None
        # 投信时校验过回答针对挂起的提问；之后提问可能已被别的运行结束（回答在它运行中途才到），那样的回答
        # 按迟到的回答作为文字交给模型（_content）
        on_time = [m for m in queued if m.kind == MailKind.ANSWER and set(m.answers) <= set(pending)]
        answers = {k: AskResult.model_validate(v) for m in on_time for k, v in m.answers.items()}
        deferred = DeferredToolResults(calls=deferred_results(pending, answers)) if pending else None
        state.taken = list(queued)
        content = await self._content(state, [m for m in queued if m not in on_time])
        # 开头的内容由 pydantic-ai 放进第一次请求的用户消息（下标从 0 起），事件在那次请求保存前标上
        state.opening_events = content.events
        return (content.items or None), deferred

    @staticmethod
    def _resumable(history: list[ModelMessage]) -> bool:
        """历史从中途续跑：停在一条没得到回复、也没被停止的请求上，或最后的回复里有没执行完的工具调用（不是挂起的提问）。

        停在提问上时不是续跑，哪怕末尾是一条请求：提问与别的工具在同一个回复里时，pydantic-ai 以挂起结束时把别的
        工具的结果追加成一条请求，这时要等回答（或跳过）作为 DeferredToolResults 交回，不能把那条请求重发出去。"""
        if not history or pending_questions(history):
            return False
        last = history[-1]
        if isinstance(last, ModelRequest):
            return not (last.metadata or {}).get(TURN_STOPPED)
        return any(c.tool_name != ASK_USER for c in unanswered_calls(history))

    async def _content(self, state: _Run, mail: Sequence[Mail]) -> _Content:
        """信件交给模型的内容：消息是原文加附件引用，事件套上来源（conventions.format_event），回答在这里只会是
        迟到的（按时的回答作为提问的结果交回，见 _opening）。"""
        items: list[UserContent] = []
        events: list[int] = []
        for m in mail:
            if m.kind == MailKind.ANSWER:
                items.append(late_answer_text([AskResult.model_validate(v) for v in m.answers.values()]))
            elif m.kind == MailKind.EVENT:
                if m.source is None:
                    # post_in 校验过事件必有来源
                    raise RuntimeError(f"event mail {m.id} has no source")
                events.append(len(items))
                items.append(format_event(m.source, m.content))
            else:
                files = await self._files.referenced(m.attachments)
                await copy_to_workspace(self._files, files, self._runtime.workspace(state.agent.id))
                items += [*([m.content] if m.content.strip() else []), *references(files)]
        return _Content(items, events)

    # ---- 每一步 ----

    async def _started(self, state: _Run, messages: list[ModelMessage]) -> None:
        state.live = messages
        state.saved = len(messages)

    async def _before_request(self, state: _Run, request: ModelRequest, messages: list[ModelMessage]) -> None:
        state.live = messages
        if state.resending:
            # pydantic-ai 取出历史末尾那条已保存的请求重发：它已经在库里，不改它、不再保存；信件留给下一次请求
            state.resending = False
            state.saved = len(messages) + 1
            return
        async with self._db.transaction() as s:
            arrived = [
                m for m in await queued_in(s, state.session.id) if m.id not in {t.id for t in state.taken}
            ]
        # 运行中途没有挂起的提问（提问只在运行结束时挂起），这时到达的回答都是迟到的
        if state.opening_events:
            _mark_events(request, 0, state.opening_events)
            state.opening_events = []
        if arrived:
            state.taken += arrived
            _append_content(request, await self._content(state, arrived))
        await self._save(state, [*messages[state.saved :], request], consumed=state.taken)
        if state.last_row is None:
            raise RuntimeError(f"run {state.run_id} saved a request without a row")
        self._live.step(state.session.id, state.last_row.id)
        state.handled += state.taken
        state.taken = []
        state.saved = len(messages) + 1

    async def _after_response(
        self, state: _Run, response: ModelResponse, messages: list[ModelMessage]
    ) -> None:
        state.live = messages
        await self._save(state, messages[state.saved :], consumed=[])
        state.saved = len(messages)

    async def _save_tail(self, state: _Run) -> None:
        if state.live is not None and len(state.live) > state.saved:
            await self._save(state, state.live[state.saved :], consumed=[])
            state.saved = len(state.live)

    async def _save(self, state: _Run, messages: Sequence[ModelMessage], *, consumed: Sequence[Mail]) -> None:
        """一步：这些消息与它们带上的信件（记为已处理）在同一个事务里提交。"""
        stored = redact_messages(list(messages))
        async with self._db.transaction() as s:
            if await s.get(Session, state.session.id) is None:
                raise RuntimeError(f"session {state.session.id} vanished during its run")
            rows = [
                Message(session_id=state.session.id, task_id=state.run_id, payload=dump_message(m))
                for m in stored
            ]
            s.add_all(rows)
            await consume_in(s, [m.id for m in consumed], state.run_id)
        log.info(
            "step saved",
            extra={
                "session_id": state.session.id,
                "run_id": state.run_id,
                "messages": len(rows),
                "consumed_mail": [m.id for m in consumed],
            },
        )
        state.messages += stored
        if rows:
            state.last_row = rows[-1]

    # ---- 停止 ----

    def stop(self, session_id: str) -> bool:
        """停止这个会话正在进行的运行；没有在运行的返回 False（调用方仍会清空信箱，见 stop_session）。"""
        return self._runs.stop(session_id)

    async def stop_session(self, session_id: str) -> None:
        """用户停止会话：停下正在进行的运行（它在 _stopped 里收尾），没有在运行时直接清空信箱、停下它发起的异步工作。"""
        if not self.stop(session_id):
            await self._halt(session_id)

    async def _stopped(self, run_id: int, session_id: str) -> RunOutcome:
        """正在进行的运行被停止：保存运行中的历史里还没保存的部分（没出结果的调用补上「被停止」的结果），再交给
        _halt 完成停止的其余部分。还没拿到运行中的历史（停在第一次请求之前）时没有要保存的，历史由 _halt 从库里补齐。"""
        state = self._active.get(session_id)
        saved = 0
        if state is not None and state.live is not None:
            tail = [*state.live[state.saved :], *_stopped_results(state.live)]
            if tail:
                tail[-1] = _marked_stopped(tail[-1])
                await self._save(state, tail, consumed=[])
                saved = len(tail)
        log.info("run stopped by user", extra={"session_id": session_id, "run_id": run_id, "saved": saved})
        await self._halt(session_id)
        if state is None:
            # 停在这次运行读出状态之前：它什么都没做
            return RunOutcome("", [], True, no_commit)
        return RunOutcome("", state.messages, True, self._finisher(state, "", stopped=True, inline=False))

    async def _halt(self, session_id: str) -> None:
        """停止会话（docs/architecture.md「停止会话」），运行中与没在运行时共用：结束它的后台任务；把历史停在一个
        不会被续跑的地方（悬空的工具调用补上「被停止」的结果，末尾没得到回复的请求标上停止）；排队与等重试的运行
        记为完成；清空信箱，被丢弃的和这些运行处理过的委派交回「被停止」；停下它 report_back 委派出去、还没交回的
        thread（先撤掉交回地址，它们不再给这个会话投信，否则刚清空的信箱又会唤醒它）。"""
        # 先停后台任务：标记同步设上，之后结束的任务不再投信
        await self._stop_jobs(session_id)
        async with self._db.transaction() as s:
            session, agent = await _load(s, session_id)
            await _close_history_in(s, session_id)
            dropped = [
                t.id for t in await self._queue.finish_pending_in(s, turn_lane(session_id), TaskKind.TURN)
            ]
            unsettled = await s.scalars(
                select(Mail).where(
                    Mail.session_id == session_id,
                    Mail.report_to.is_not(None),
                    or_(Mail.state == MailState.QUEUED, Mail.consumed_by.in_(dropped)),
                )
            )
            for mail in unsettled.all():
                await settle_in(
                    s, self._queue, mail, session, finished_text(session, agent, "", stopped=True)
                )
            discarded = await discard_in(s, session_id)
            children = await _reporting_threads(s, session_id)
            for mail in await _reports_to(s, session_id):
                mail.report_to = None
        log.info(
            "session halted",
            extra={
                "session_id": session_id,
                "dropped_runs": dropped,
                "discarded_mail": discarded,
                "stopped_threads": children,
            },
        )
        for child in children:
            await self.stop_session(child)

    # ---- 收尾 ----

    def _finisher(self, state: _Run, answer: str, *, stopped: bool, inline: bool) -> Commit:
        async def finish(s: AsyncSession) -> None:
            session, agent = await _load(s, state.session.id)
            # 停止时开头取走、还没交给模型的信件随信箱清空，由 _halt 交回结局
            handled = state.handled
            if state.messages:
                session.updated_at = utcnow()
                session.unread = not session.private
                await self._count_run(s, agent)
            # 从库里读：交回地址可能已被撤掉（发起方被停止，或最终失败时已经交回过），手里的信件对象不会跟着变
            reports = await s.scalars(
                select(Mail).where(Mail.id.in_([m.id for m in handled]), Mail.report_to.is_not(None))
            )
            for mail in reports.all():
                await settle_in(
                    s, self._queue, mail, session, finished_text(session, agent, answer, stopped=stopped)
                )
            if not stopped and state.messages:
                await self._compaction.after_turn(s, session, agent.model, state.messages)
                if any(m.awaited for m in handled) and not inline:
                    await self._notifier.enqueue_in(
                        s,
                        agent.id,
                        Notification(
                            title=f"{agent.name} · {session.title}", body=answer, session_id=session.id
                        ),
                    )
            await wake_in(s, self._queue, session, finished=state.run_id)

        return finish

    async def _count_run(self, s: AsyncSession, agent: Agent) -> None:
        """agent 级运行计数；达到阈值时把 (检查点, 当前最大消息 id] 交给反思。"""
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

    # ---- 私有 thread ----

    async def run_inline(self, session_id: str, prompt: str) -> Answer:
        """在发问方这一步里跑完私有 thread，返回给发问 agent 的回答（连同挂给用户的文件）。

        和队列里的运行同一条规则：一次运行结束、信箱里还有信件就再跑一次，直到信箱空（私有 thread 不被 wake_in
        唤醒，这条规则在这里执行）。例如上一次内联运行失败、这次从它保存的请求续跑时，新问题要等下一次运行才交给模型。"""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            async with self._db.transaction() as s:
                await post_in(
                    s, self._queue, session_id, kind=MailKind.MESSAGE, content=prompt, awaited=False
                )
            messages: list[ModelMessage] = []
            while True:
                async with self._db.transaction() as s:
                    before = {m.id for m in await queued_in(s, session_id)}
                outcome = await self._run_inline_once(session_id)
                messages += outcome.messages
                async with self._db.transaction() as s:
                    left = {m.id for m in await queued_in(s, session_id)}
                if not left:
                    break
                if before <= left and not outcome.messages:
                    # 一次运行什么都没做（没有处理掉信件，也没有续跑出新的消息）、信箱又不空：再跑也一样，在这里报出而不是
                    # 无限循环。续跑重发保存的请求时不处理新信件，但有新的回复，算有进展
                    raise RuntimeError(
                        f"inline run of {session_id} handed none of mail {sorted(left)} to the model"
                    )
        return Answer.of_run(outcome.answer, messages)

    async def _run_inline_once(self, session_id: str) -> RunOutcome:
        async with self._db.transaction() as s:
            session, _agent = await _load(s, session_id)
            run = self._queue.start_inline(s, agent_id=session.agent_id, session_id=session_id)
            await s.flush()
            run_id = run.id
        log.info("inline run started", extra={"session_id": session_id, "run_id": run_id})
        try:
            outcome = await self.run(run_id, session_id, inline=True)
            await self._queue.complete(run_id, outcome.finish)
        except BaseException as e:
            error = (
                f"{type(e).__name__}: {e}"
                if not isinstance(e, asyncio.CancelledError)
                else "stopped with the asking run"
            )
            log.warning("inline run failed: %s", error, extra={"session_id": session_id, "run_id": run_id})
            await self._queue.fail(run_id, error, retryable=False, on_final=_nothing)
            self._publish(run_id, session_id, TaskStatus.FAILED, error)
            raise
        self._publish(run_id, session_id, TaskStatus.DONE, None)
        return outcome

    def _publish(self, run_id: int, session_id: str, status: TaskStatus, error: str | None) -> None:
        self._bus.publish(
            task_event(
                task_id=run_id,
                kind=TaskKind.TURN,
                status=status,
                session_id=session_id,
                agent_id=None,
                error=error,
            )
        )


async def _nothing(_s: AsyncSession, _task: Task) -> None:
    """内联运行失败：发问方那一步随之失败，由它的运行处理。"""


@dataclass(frozen=True)
class _Content:
    """一批信件交给模型的内容段，以及其中哪些段是事件（下标）。"""

    items: list[UserContent]
    events: list[int]


def _stopped_results(history: list[ModelMessage]) -> list[ModelMessage]:
    """没出结果的工具调用（挂起的提问除外）补上「被用户停止、可能已部分执行」的结果；没有则为空。"""
    dangling = [c for c in unanswered_calls(history) if c.tool_name != ASK_USER]
    if not dangling:
        return []
    return [
        ModelRequest(
            parts=[
                ToolReturnPart(c.tool_name, STOPPED_TOOL_RESULT, tool_call_id=c.tool_call_id)
                for c in dangling
            ]
        )
    ]


def _marked_stopped(message: ModelMessage) -> ModelMessage:
    """标上「已停止」（TURN_STOPPED）：界面据此显示，续跑据此不重发这条请求。"""
    return replace(message, metadata={**(message.metadata or {}), TURN_STOPPED: True})


async def _close_history_in(s: AsyncSession, session_id: str) -> None:
    """停止时让会话历史停在不会被续跑的地方（唯一事实源是库里的历史，不依赖运行中的状态）：悬空的工具调用补上
    「被停止」的结果，末尾没得到回复、也没标停止的请求标上停止。运行中被停止时 _stopped 已经保存了这些，这里没事可做。"""
    history = (await load_context(s, session_id)).messages
    if closing := _stopped_results(history):
        run = await latest_run(s, session_id)
        if run is None:
            raise RuntimeError(f"session {session_id} has tool calls but no run")
        s.add(
            Message(session_id=session_id, task_id=run.id, payload=dump_message(_marked_stopped(closing[0])))
        )
        return
    if (
        history
        and isinstance(history[-1], ModelRequest)
        and not (history[-1].metadata or {}).get(TURN_STOPPED)
    ):
        row = await s.scalar(
            select(Message).where(Message.session_id == session_id).order_by(Message.id.desc()).limit(1)
        )
        if row is None:
            raise RuntimeError(f"session {session_id} has history but no message rows")
        (message,) = load_messages([row.payload])
        row.payload = dump_message(_marked_stopped(message))


def _append_content(request: ModelRequest, content: _Content) -> None:
    """把内容并进这条请求唯一的用户消息（没有就加一条）：一条请求里的信件都在同一段用户消息里，事件下标才有意义。"""
    for i, part in enumerate(request.parts):
        if isinstance(part, UserPromptPart):
            existing = [part.content] if isinstance(part.content, str) else list(part.content)
            request.parts[i] = replace(part, content=[*existing, *content.items])
            _mark_events(request, len(existing), content.events)
            return
    request.parts.append(UserPromptPart(content.items))
    _mark_events(request, 0, content.events)


def _mark_events(request: ModelRequest, offset: int, events: list[int]) -> None:
    """在请求上记下用户消息里哪些段是事件（TURN_EVENTS），界面把它们单独显示。"""
    if events:
        marked = event_indices(request)
        request.metadata = {**(request.metadata or {}), TURN_EVENTS: [*marked, *(offset + i for i in events)]}


async def _adopted(s: AsyncSession, session_id: str, run_id: int) -> list[Mail]:
    """从历史中途续跑、而停下的那部分是另一次运行保存的（它失败了，之后有新信件唤醒了这次运行）：这次运行接着做完
    它的工作，也接管它处理过的信件——推送、交回委派的结果由这次运行做。之后再重试那次运行时什么都不做
    （Consumer._run 的「没有要交给模型的」）。"""
    tail_run = await s.scalar(
        select(Message.task_id).where(Message.session_id == session_id).order_by(Message.id.desc()).limit(1)
    )
    if tail_run is None or tail_run == run_id:
        return []
    return list((await s.scalars(select(Mail).where(Mail.consumed_by == tail_run))).all())


async def _reports_to(s: AsyncSession, session_id: str) -> list[Mail]:
    return list((await s.scalars(open_reports(session_id))).all())


async def _reporting_threads(s: AsyncSession, session_id: str) -> list[str]:
    """这个会话 report_back 委派出去、结果还没交回的 thread。"""
    return list(dict.fromkeys(m.session_id for m in await _reports_to(s, session_id)))


class RunHandler:
    """队列里的运行（turn task）。"""

    def __init__(self, consumer: Consumer, queue: TaskQueue):
        self._consumer = consumer
        self._queue = queue

    async def execute(self, task: Task) -> Commit:
        if task.session_id is None:
            raise RuntimeError(f"run {task.id} has no session")
        return (await self._consumer.run(task.id, task.session_id)).finish

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """最终失败：这次运行没能处理完的委派把失败原因交给发起方，它不会一直等。包括它处理过的，和交给过它、还排着的
        ——运行在第一次保存之前就失败时，开头取走的信件一直排着，也不会再有运行因为它们被唤醒（mailbox.handed_to）；
        它们随失败交回一起退出信箱，不会在之后的运行里被悄悄执行。运行开始之后才到的不动：它们会唤醒新的运行。"""
        if task.session_id is None:
            raise RuntimeError(f"run {task.id} has no session")
        handled = (
            await s.scalars(select(Mail).where(Mail.consumed_by == task.id, Mail.report_to.is_not(None)))
        ).all()
        stranded = [
            m for m in await queued_in(s, task.session_id) if m.report_to is not None and handed_to(m, task)
        ]
        if not handled and not stranded:
            return
        child, agent = await _load(s, task.session_id)
        for mail in [*handled, *stranded]:
            await settle_in(s, self._queue, mail, child, failed_text(child, agent, failure_of(task)))
        await discard_in(s, task.session_id, stranded)
