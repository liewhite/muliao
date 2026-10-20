"""逐步保存：一次运行的每一步（一次模型请求及其返回的工具调用）各自提交。请求模型之前把新到的信件并进这条请求，请求与
它带上的信件（记为已处理）同一个事务提交；模型回复之后提交这条回复。失败或重启后从上一步续跑，已经保存的步骤不会
重放（只有中断那一步里正在执行的工具会再执行一遍）。实时展示按步换段（bus.LiveRuns.step）。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

from muliao.bus import LiveRuns
from muliao.db import Database
from muliao.models import Agent, Mail, Message, Session
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import Compactor
from muliao.runtime.content import ContentBuilder, append_content, mark_events
from muliao.runtime.history import marked_stopped, stopped_results
from muliao.runtime.mailbox import consume_in, queued_in
from muliao.runtime.redaction import redact_messages
from muliao.runtime.runner import StepHooks

log = logging.getLogger(__name__)


class CompactionDue(Exception):
    """上下文超过压缩阈值：这次请求已经保存为一步，停下驱动，由消费者压缩后从它续跑（runtime.compaction）。"""


@dataclass
class RunState:
    """一次运行在进行中的状态（只在这次运行里用）。"""

    run_id: int
    session: Session
    agent: Agent
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
    # 这次运行最近一次压缩之后，上下文里的模型回复数：在它之后出现的回复，token 数才反映压缩后的上下文
    compacted_at: int | None = None
    # 压缩不下去了（保留窗口之外没有可压缩的，或压缩后的第一次回复仍超阈值）：这次运行不再因为上下文大小停下
    compaction_exhausted: bool = False

    def after_compaction(self, history: Sequence[ModelMessage], compacted: int) -> None:
        """压缩过（compacted 为压缩的步数）、重新加载了上下文之后调用。"""
        self.compacted_at = responses_in(history)
        if not compacted:
            self.compaction_exhausted = True


def responses_in(history: Sequence[ModelMessage]) -> int:
    return sum(isinstance(m, ModelResponse) for m in history)


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
            # 全部已经保存：被停止时没有运行中的部分要补，停止从库里补齐历史（runtime.halt）
            state.live = None
            raise CompactionDue()

    def _compaction_due(self, state: RunState, messages: list[ModelMessage]) -> bool:
        """上下文超过阈值、该停下压缩了。只认最近一次压缩之后的回复报告的 token 数（之前的反映的是压缩前的上下文）；
        压缩后的第一次回复仍超阈值，说明保留窗口本身就超了，再压也压不下去。"""
        if state.compaction_exhausted:
            return False
        responses = responses_in(messages)
        if state.compacted_at is not None and responses <= state.compacted_at:
            return False
        if not self._compactor.over_threshold(state.session.id, messages):
            return False
        if state.compacted_at is not None and responses == state.compacted_at + 1:
            log.warning(
                "context still over the compaction threshold right after compacting; not compacting again this run",
                extra={"session_id": state.session.id, "run_id": state.run_id},
            )
            state.compaction_exhausted = True
            return False
        return True

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

    async def save_stopped(self, state: RunState) -> int:
        """运行被停止：保存运行中的历史里还没保存的部分，没出结果的调用补上「被停止」的结果，最后一条标上停止。
        还没拿到运行中的历史（停在第一次请求之前）时没有要保存的，历史由停止从库里补齐（runtime.halt）。返回保存的条数。"""
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
