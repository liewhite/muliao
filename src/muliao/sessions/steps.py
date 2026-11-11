"""逐步保存：一次运行的每一步（一次模型请求及其返回的工具调用）各自提交。请求模型之前把新到的信件并进这条请求，请求与
它带上的信件（记为已处理）同一个事务提交；模型回复之后提交这条回复。失败或重启后从上一步续跑，已经保存的步骤不会
重放；中断那一步里正在执行的工具也不重新执行，而是补上「被中断」的结果（sessions.consumer，经 save_in）。停在挂起的调用上时最后
一步的工具结果在运行结束时和等批准的记录一起保存（save_end）。实时展示按步换段
（sessions.live.LiveSegments.step）。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import Redaction, StepHooks, with_notices
from muliao.agents.api import AgentDir, AgentInfo
from muliao.platform.db import Database, utcnow
from muliao.platform.text_edit import truncate
from muliao.platform.tool_views import ToolRenders, truncated
from muliao.sessions.approvals import record_in as record_approvals_in
from muliao.sessions.compaction import Compactor, Verdict
from muliao.sessions.content import Content, ContentBuilder, append_content, mark_events
from muliao.sessions.conventions import attach_run_context
from muliao.sessions.history import SessionContext, append_in, latest_compaction, load_context
from muliao.sessions.live import LiveSegments
from muliao.sessions.mailbox import consume_in, queued_in
from muliao.sessions.tables import Mail, Message, Session
from muliao.sessions.transcript import Item, call_item, result_item

log = logging.getLogger(__name__)

_PREVIEW_CHARS = 2000


@dataclass(frozen=True)
class Opening:
    """开头取走的信件与它们交给模型的内容（pydantic-ai 把内容放进第一次请求的用户消息）。"""

    mail: list[Mail]
    content: Content


class CompactionDue(Exception):
    """上下文超过压缩阈值：这次请求已经保存为一步，停下驱动，由消费者压缩后从它续跑（sessions.compaction）。"""


@dataclass
class RunState:
    """一次运行在进行中的状态（只在这次运行里用）。消费者建它，之后只由 Steps 改。"""

    run_id: int
    session: Session
    agent: AgentInfo
    # 发给模型的会话上下文：摘要与切点之后的历史，保存一步就接上这一步，压缩后从库里重新加载（Steps.compact）。
    # 运行从它续跑，压缩按它判断（Steps.compaction_due）
    context: SessionContext
    # 这次运行处理过的信件：包括之前几次尝试里已经保存的（重试沿用同一个运行），以及接着别的运行保存的部分续跑时
    # 那次运行处理过的（consumer._adopted）
    handled: list[Mail]
    # 这次运行保存的消息（包括之前几次尝试保存的），和最后保存的那一行
    messages: list[ModelMessage]
    last_row: Message | None
    # 运行交出、还没保存的消息（agent_runtime.StepHooks.took，含这次请求）：一步保存提交前的那几条，和驱动结束时交出的
    # 末尾。只经 save_handed_in 保存（每一步、被停止时、停在挂起的调用上时）
    unsaved: list[ModelMessage] = field(default_factory=list)
    # 内容已在 unsaved 里那条请求上的信件：随它保存时记为已处理
    taken: list[Mail] = field(default_factory=list)
    # 开头取走的信件与交给模型的内容（Steps.begin）：第一条请求交出时随它进入 taken，并在请求里标出哪些段是事件、
    # 检查。还没交出请求就被停止时，它们没交给模型，由停止丢弃
    opening: Opening | None = None
    # 这次驱动的工具组在连接时发现的问题（RunScope.notices）：和运行上下文挂在同一条请求上（instructions.with_notices）。
    # 每次驱动（压缩后续跑会重新连接）由 started 覆盖，挂出去之后清空
    notices: Sequence[str] = ()
    # 挂着这次运行的运行上下文的那条请求（消息 id）：还没挂，或运行中途压缩把它切进了摘要时为 None，下一条新请求
    # 挂一段（时间取那时）
    run_context_row: int | None = None
    # 压缩不下去了（保留窗口之外没有可压缩的，或刚压缩过仍超阈值）：这次运行不再因为上下文大小停下
    compaction_exhausted: bool = False


class Steps:
    def __init__(
        self,
        db: Database,
        live: LiveSegments,
        content: ContentBuilder,
        compactor: Compactor,
        renders: ToolRenders,
        # 消息落库与实时展示之前的脱敏（Toolbox.redaction）
        redaction: Redaction,
    ):
        self._db = db
        self._renders = renders
        self._redaction = redaction
        self._live = live
        self._content = content
        self._compactor = compactor

    def hooks(self, state: RunState, run_context: Callable[[], str]) -> StepHooks:
        """这次驱动的回调。run_context() 给出一段运行上下文（当前时间取调用时），挂在这次运行新增的第一条请求上。"""
        return StepHooks(
            started=lambda notices: self._started(state, notices),
            took=lambda new: self._took(state, new),
            before_request=lambda request: self._before_request(state, request, run_context),
            after_response=lambda: self._save(state),
            on_text=lambda kind, text: self._live.publish_delta(
                state.session.id, state.session.agent_id, kind, text
            ),
            on_tool_call=lambda part: self._live.publish_item(
                state.session.id,
                state.session.agent_id,
                _preview(call_item(self._redaction.call(part), utcnow(), self._renders)),
            ),
            on_tool_result=lambda part: self._live.publish_item(
                state.session.id,
                state.session.agent_id,
                _preview(
                    result_item(
                        self._redaction.retry(part) if isinstance(part, RetryPromptPart) else part,
                        self._renders,
                    )
                ),
            ),
        )

    async def _started(self, state: RunState, notices: Sequence[str]) -> None:
        state.notices = notices

    def begin(self, state: RunState, taken: Sequence[Mail], opening: Content) -> None:
        """开头取走的信件与交给模型的内容（sessions.consumer 交出之后）：第一条请求交出时随它进入 taken（_took）。"""
        state.opening = Opening(list(taken), opening)

    def _took(self, state: RunState, new: list[ModelMessage]) -> None:
        """运行交出的消息（同步，与运行记下「交出到哪」之间没有挂起点）。第一条请求里是开头的内容：在它上面标出事件（pydantic-ai
        把内容放进用户消息，下标从 0 起），开头的信件随它进入 taken。"""
        state.unsaved.extend(new)
        request = next((m for m in new if isinstance(m, ModelRequest)), None)
        if state.opening is not None and request is not None:
            mark_events(request, 0, state.opening.content)
            state.taken += state.opening.mail
            state.opening = None

    async def _before_request(
        self, state: RunState, request: ModelRequest, run_context: Callable[[], str]
    ) -> None:
        """request 已经交出（在 unsaved 里）：往里并进新到的信件与运行上下文，再把已交出的一起保存。重发历史末尾那条已保存
        的请求时不回调这里（agent_runtime.StepHooks）：它已经在库里，不改它；信件与运行上下文留给下一次请求。

        在这里被停止时，停止把已交出的（这条请求与它带着的上一步的工具结果）原样存库：等内容的那几步还没改请求；
        并进信件与记进 taken 之间没有挂起点，存下的请求里有哪些信件，就记哪些为已处理。"""
        async with self._db.transaction() as s:
            taken = {t.id for t in state.taken}
            arrived = [m for m in await queued_in(s, state.session.id) if m.id not in taken]
        # 运行中途没有挂起的调用（调用只在运行结束时挂起），这时到达的回答都是迟到的
        content = await self._content.of(state.agent.id, arrived) if arrived else None
        if content is not None:
            append_content(request, content)
            state.taken += arrived
        attaching = state.run_context_row is None
        context = with_notices(run_context() if attaching else "", state.notices)
        if context:
            # 只挂在本次运行新增的第一条请求上（重发的那条已经在库里，不改它——改了它，它之前的历史就不再是同一个
            # 前缀，缓存全废）。运行上下文已经挂出去了（压缩后续跑）时，notices 单独挂在下一条新请求上
            attach_run_context(request, context)
        await self._save(state)
        if state.last_row is None:
            raise RuntimeError(f"run {state.run_id} saved a request without a row")
        if attaching:
            state.run_context_row = state.last_row.id
        state.notices = ()
        self._live.step(state.session.id, state.last_row.id)
        if self.compaction_due(state):
            # 全部已经保存：被停止时没有运行中的部分要补，停止从库里补齐历史（sessions.halt）
            raise CompactionDue()

    def compaction_due(self, state: RunState) -> bool:
        """这次运行该停下压缩了吗（规则见 Compactor.verdict）：运行开头与每次请求保存之后都问这里。压缩不下去时这次运行
        不再判断。"""
        if state.compaction_exhausted:
            return False
        verdict = self._compactor.verdict(
            state.session, state.context.messages, state.context.stale_responses
        )
        if verdict == Verdict.HOPELESS:
            state.compaction_exhausted = True
        return verdict == Verdict.DUE

    async def compact(self, state: RunState, config: AgentDir) -> None:
        """压缩（compaction_due 之后；运行中途时触发它的请求已经保存）：用这次运行的配置快照（config）压缩，从库里重新
        加载上下文（摘要 + 切点之后）作为 state.context，运行从它续跑。挂着运行上下文的那条请求被压进了摘要时，模型不再
        看得到它：下一条新请求重新挂一段。"""
        compacted = await self._compactor.compact(state.session, config)
        async with self._db.transaction() as s:
            state.context = await load_context(s, state.session.id)
            compaction = await latest_compaction(s, state.session.id)
        if (
            compaction is not None
            and state.run_context_row is not None
            and state.run_context_row <= compaction.through_message_id
        ):
            state.run_context_row = None
        state.compaction_exhausted = compacted == 0

    async def save_end(self, state: RunState, requested: Mapping[str, str]) -> None:
        """运行结束（还登记着可停止）：停在挂起的调用上时，挂起之外已经执行完的工具结果在最后一条请求里，和这次新请求
        批准的调用（requested：调用 id -> pin，sessions.approvals）同一个事务提交。分开提交时，进程在两者之间退出会留下一个
        没有结果、又不算在等批准的调用；放到运行注销之后（收尾事务），这中间到的停止又会对着缺了结尾的历史补结果。"""
        async with self._db.transaction() as s:
            await self.save_handed_in(s, state)
            if requested:
                await record_approvals_in(s, state.session.id, requested)

    async def _save(self, state: RunState) -> None:
        """一步：在自己的事务里保存已交出的（save_handed_in）。提交之后到清空 unsaved 之间没有挂起点，被停止时不会
        把已提交的再存一遍：原生取消只在事务之外发出（sessions.stopping），提交后只有登记的 when_committed 动作会
        await（platform.db），而 save_in 不登记——给它加上了就要在这里重新考虑重复保存。"""
        async with self._db.transaction() as s:
            await self.save_handed_in(s, state)

    async def save_handed_in(self, s: AsyncSession, state: RunState) -> None:
        """在调用方事务里保存运行已交出、还没保存的消息（state.unsaved），与内容在其中的信件（state.taken，记为已处理）。
        运行交出的消息只经这里保存：每一步（_save）、正常结束时和等批准的记录一起（save_end）、被停止时在停止的事务里
        （sessions.halt 的 first：当前这一步已产生的部分——包括生成到一半的回答、还没发出的请求——原样存库，没出结果的调用
        与停止标记由同一事务里的停止从库里补齐）。"""
        if not state.unsaved:
            if state.taken:
                raise RuntimeError(
                    f"run {state.run_id} took mail {[m.id for m in state.taken]} without a request"
                )
            return
        await self.save_in(s, state, list(state.unsaved), consumed=state.taken)
        state.unsaved.clear()
        state.handled += state.taken
        state.taken = []

    async def save_in(
        self, s: AsyncSession, state: RunState, messages: Sequence[ModelMessage], *, consumed: Sequence[Mail]
    ) -> None:
        """在调用方事务里把这次运行的这些消息接在历史末尾，并把它们带上的信件记为已处理。运行保存消息只经这里。"""
        stored = self._redaction.messages(messages)
        if await s.get(Session, state.session.id) is None:
            raise RuntimeError(f"session {state.session.id} vanished during its run")
        rows = await append_in(s, state.session.id, state.run_id, stored)
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
        state.context = replace(state.context, messages=[*state.context.messages, *stored])
        if rows:
            state.last_row = rows[-1]


def _preview(item: Item) -> Item:
    """实时展示里工具的参数与结果截断（保存后历史里是完整的），展示块里的文字同样截断。"""
    return item.model_copy(
        update={
            "text": truncate(item.text, _PREVIEW_CHARS),
            "view": None if item.view is None else truncated(item.view, _PREVIEW_CHARS),
        }
    )
