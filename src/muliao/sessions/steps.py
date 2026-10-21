"""逐步保存：一次运行的每一步（一次模型请求及其返回的工具调用）各自提交。请求模型之前把新到的信件并进这条请求，请求与
它带上的信件（记为已处理）同一个事务提交；模型回复之后提交这条回复。失败或重启后从上一步续跑，已经保存的步骤不会
重放；中断那一步里正在执行的工具也不重新执行，而是补上「被中断」的结果（save_interrupted）。实时展示按步换段
（sessions.live.LiveRuns.step）。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

from muliao.agent_runtime.api import StepHooks, redact_messages
from muliao.agents.api import AgentInfo
from muliao.platform.db import Database
from muliao.platform.text_edit import truncate
from muliao.sessions.codec import dump_message
from muliao.sessions.compaction import Compactor, Verdict
from muliao.sessions.content import ContentBuilder, append_content, mark_events
from muliao.sessions.history import marked_stopped, stopped_results
from muliao.sessions.live import LiveRuns
from muliao.sessions.mailbox import consume_in, queued_in
from muliao.sessions.tables import Mail, Message, Session
from muliao.sessions.transcript import Item, call_item, result_item

log = logging.getLogger(__name__)

_PREVIEW_CHARS = 2000


class CompactionDue(Exception):
    """上下文超过压缩阈值：这次请求已经保存为一步，停下驱动，由消费者压缩后从它续跑（sessions.compaction）。"""


@dataclass
class RunState:
    """一次运行在进行中的状态（只在这次运行里用）。"""

    run_id: int
    session: Session
    agent: AgentInfo
    # 从历史末尾那条没得到回复的请求续跑：第一次请求是重发它，它已经在库里
    resending: bool
    # 运行中的历史里已经保存的条数（started 时按 pydantic-ai 规整后的历史定基准）
    saved: int = 0
    # 运行中的历史本身（pydantic-ai 的 ctx.state.message_history）：被停止时从它取回没保存的部分
    live: list[ModelMessage] | None = None
    # 交给模型、等这一步保存时记为已处理的信件
    taken: list[Mail] = field(default_factory=list)
    # 开头交给模型的内容里哪些段是事件，第一次请求保存前标上
    opening_events: list[int] = field(default_factory=list)
    # 这次运行处理过的信件：包括之前几次尝试里已经保存的（重试沿用同一个运行），以及接着别的运行保存的部分续跑时
    # 那次运行处理过的（consumer._adopted）
    handled: list[Mail] = field(default_factory=list)
    # 这次运行保存的消息（包括之前几次尝试保存的），和最后保存的那一行
    messages: list[ModelMessage] = field(default_factory=list)
    last_row: Message | None = None
    # 上下文里最近一次压缩之前就有的模型回复条数（sessions.history.SessionContext.stale_responses），加载上下文时更新
    stale_responses: int | None = None
    # 压缩不下去了（保留窗口之外没有可压缩的，或刚压缩过仍超阈值）：这次运行不再因为上下文大小停下
    compaction_exhausted: bool = False


class Steps:
    def __init__(self, db: Database, live: LiveRuns, content: ContentBuilder, compactor: Compactor):
        self._db = db
        self._live = live
        self._content = content
        self._compactor = compactor

    def hooks(self, state: RunState) -> StepHooks:
        return StepHooks(
            started=lambda messages: self._started(state, messages),
            before_request=lambda request, messages: self._before_request(state, request, messages),
            after_response=lambda response, messages: self._after_response(state, response, messages),
            on_text=lambda kind, text: self._live.publish_delta(
                state.session.id, state.session.agent_id, kind, text
            ),
            on_tool_call=lambda part: self._live.publish_item(
                state.session.id, state.session.agent_id, _preview(call_item(part))
            ),
            on_tool_result=lambda part: self._live.publish_item(
                state.session.id, state.session.agent_id, _preview(result_item(part))
            ),
        )

    async def _started(self, state: RunState, messages: list[ModelMessage]) -> None:
        state.live = messages
        state.saved = len(messages)

    async def _before_request(
        self, state: RunState, request: ModelRequest, messages: list[ModelMessage]
    ) -> None:
        state.live = messages
        if state.resending:
            # pydantic-ai 取出历史末尾那条已保存的请求重发：它已经在库里，不改它、不再保存；信件留给下一次请求
            state.resending = False
            state.saved = len(messages) + 1
            return
        async with self._db.transaction() as s:
            taken = {t.id for t in state.taken}
            arrived = [m for m in await queued_in(s, state.session.id) if m.id not in taken]
        if state.opening_events:
            mark_events(request, 0, state.opening_events)
            state.opening_events = []
        if arrived:
            # 运行中途没有挂起的调用（调用只在运行结束时挂起），这时到达的回答都是迟到的
            state.taken += arrived
            append_content(request, await self._content.of(state.agent.id, arrived))
        await self._save(state, [*messages[state.saved :], request], consumed=state.taken)
        if state.last_row is None:
            raise RuntimeError(f"run {state.run_id} saved a request without a row")
        self._live.step(state.session.id, state.last_row.id)
        state.handled += state.taken
        state.taken = []
        state.saved = len(messages) + 1
        if self._compaction_due(state, messages):
            # 全部已经保存：被停止时没有运行中的部分要补，停止从库里补齐历史（sessions.halt）
            state.live = None
            raise CompactionDue()

    def _compaction_due(self, state: RunState, messages: list[ModelMessage]) -> bool:
        """该停下压缩了（规则见 Compactor.verdict）。压缩不下去时这次运行不再判断。"""
        if state.compaction_exhausted:
            return False
        verdict = self._compactor.verdict(state.session.id, messages, state.stale_responses)
        if verdict == Verdict.HOPELESS:
            state.compaction_exhausted = True
        return verdict == Verdict.DUE

    async def _after_response(
        self, state: RunState, response: ModelResponse, messages: list[ModelMessage]
    ) -> None:
        state.live = messages
        await self._save(state, messages[state.saved :], consumed=[])
        state.saved = len(messages)

    async def save_tail(self, state: RunState) -> None:
        """运行停在挂起的调用上时，挂起之外已经执行完的工具结果在最后一条请求里，一起保存。"""
        if state.live is not None and len(state.live) > state.saved:
            await self._save(state, state.live[state.saved :], consumed=[])
            state.saved = len(state.live)

    async def save_interrupted(self, state: RunState, messages: Sequence[ModelMessage]) -> int:
        """从被中断的一步续跑：给没出结果的调用补上的结果（sessions.history.interrupted_results）作为这次运行的
        第一步存下来，走的是和别的步骤同一条保存路径，历史里不留悬空的调用。返回保存的最后一行的 id。"""
        await self._save(state, messages, consumed=[])
        if state.last_row is None:
            raise RuntimeError(f"run {state.run_id} saved interrupted tool results without a row")
        return state.last_row.id

    async def save_stopped(self, state: RunState) -> int:
        """运行被停止：保存运行中的历史里还没保存的部分，没出结果的调用补上「被停止」的结果，最后一条标上停止。
        还没拿到运行中的历史（停在第一次请求之前）时没有要保存的，历史由停止从库里补齐（sessions.halt）。返回保存的条数。"""
        if state.live is None:
            return 0
        tail = [*state.live[state.saved :], *stopped_results(state.live)]
        if tail:
            tail[-1] = marked_stopped(tail[-1])
            # 开头取走、还没随请求交给模型的信件（state.taken）不算处理过，随信箱清空
            await self._save(state, tail, consumed=[])
        return len(tail)

    async def _save(
        self, state: RunState, messages: Sequence[ModelMessage], *, consumed: Sequence[Mail]
    ) -> None:
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


def _preview(item: Item) -> Item:
    """实时展示里工具的参数与结果截断（保存后历史里是完整的）。"""
    return item.model_copy(update={"text": truncate(item.text, _PREVIEW_CHARS)})
