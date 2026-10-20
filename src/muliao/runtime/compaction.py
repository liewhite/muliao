"""会话上下文压缩，与模型提供方无关（pydantic-ai 只为部分提供方提供原生压缩，按提供方分支会有两套语义）。

- 触发：发给模型的上下文（上一次模型请求的输入 token，pydantic-ai 已计入缓存 token）超过阈值时，由消费者在步与步
  之间压缩：运行开始时，以及每次请求模型之前（这次请求照常保存为一步，然后停下驱动、压缩、从库里重新加载上下文
  续跑，pydantic-ai 重发末尾那条请求）。也可由用户在会话页手动触发（同一个会话 lane 上的一个 task，调用同一个方法）。
- 执行：把最近 keep_steps 步之前的消息连同上一份摘要交给该 agent 的模型总结，写入 compactions 表。切点在步与步
  之间：之前的消息里没有悬空调用的地方，工具调用与其结果、挂起的调用与之后的回答不会被拆开；一次运行中途也有切点
  （每一批工具结果之后），长任务在运行中也能压缩。单次只取总长不超过上限的最早若干步，剩下的接着压缩，直到保留
  窗口之外没有可压缩的。
- 使用：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息（runtime.history）。原始消息不删除。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage, ModelResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.conventions import unanswered_calls
from muliao.db import Database
from muliao.models import Agent, Compaction, Message, Session, Task, TaskKind, TaskStatus
from muliao.runtime.codec import load_messages
from muliao.runtime.history import latest_compaction, rows_after
from muliao.runtime.runner import AgentRuntime
from muliao.runtime.tasks import Commit, TaskQueue, no_commit, session_lane
from muliao.transcript import to_items, to_text

log = logging.getLogger(__name__)

_ITEM_CHARS = 4000
_TRANSCRIPT_CHARS = 400_000

COMPACTION_INSTRUCTIONS = """\
You compress the earlier part of a conversation between a user and an AI agent so the agent can continue \
without the original messages. Write a dense summary in the conversation's language covering: the user's goals \
and requests, decisions made, facts learned, results of tool calls that still matter (file paths, IDs, URLs, \
commands, numbers), work completed, and open tasks or questions. Omit pleasantries and anything superseded. \
Output only the summary."""


def context_tokens(history: Sequence[ModelMessage]) -> int | None:
    """上一次模型请求的输入 token 数，即发给模型的上下文的实际大小；还没有回复时为 None。"""
    for message in reversed(history):
        if isinstance(message, ModelResponse):
            return message.usage.input_tokens
    return None


def steps_of(rows: Sequence[Message]) -> list[list[Message]]:
    """按步分组，保持时间顺序。之前的消息里没有悬空调用的地方开始新的一步：工具调用与其结果、挂起的调用与之后的
    回答总在同一组里，切点落在组与组之间，留下的结果一定找得到它的调用。留下的部分可能从一条模型回复开始，发给
    模型时补一条接续的用户消息（runtime.history.load_context）。"""
    steps: list[list[Message]] = []
    messages: list[ModelMessage] = []
    for row in rows:
        (message,) = load_messages([row.payload])
        if steps and unanswered_calls(messages):
            steps[-1].append(row)
        else:
            steps.append([row])
        messages.append(message)
    return steps


def _step_text(step: Sequence[Message]) -> str:
    return to_text(to_items(load_messages(r.payload for r in step)), max_item_chars=_ITEM_CHARS)


def select_steps(
    steps: list[list[Message]], keep_steps: int, max_chars: int
) -> tuple[list[list[Message]], bool]:
    """从最早的可压缩步开始，取总长不超过 max_chars 的若干步（至少一步）。

    返回 (这次压缩的步, 是否还有可压缩的留给下一次)。不截断内容：超出的下一次再压缩。"""
    compactable = steps[:-keep_steps]
    chosen: list[list[Message]] = []
    total = 0
    for step in compactable:
        size = len(_step_text(step))
        if chosen and total + size > max_chars:
            break
        chosen.append(step)
        total += size
    return chosen, len(chosen) < len(compactable)


def compaction_prompt(previous_summary: str | None, steps: list[list[Message]]) -> str:
    transcript = "\n".join(_step_text(step) for step in steps)
    previous = f"<previous_summary>\n{previous_summary}\n</previous_summary>\n\n" if previous_summary else ""
    return f"{previous}<conversation>\n{transcript}\n</conversation>\n\nWrite the updated summary."


class Compactor:
    def __init__(
        self, db: Database, runtime: AgentRuntime, queue: TaskQueue, *, threshold_tokens: int, keep_steps: int
    ):
        self._db = db
        self._runtime = runtime
        self._queue = queue
        self._threshold = threshold_tokens
        self._keep_steps = keep_steps

    def over_threshold(self, session_id: str, history: Sequence[ModelMessage]) -> bool:
        """发给模型的上下文是否超过阈值。"""
        tokens = context_tokens(history)
        if tokens is None:
            return False
        if tokens == 0:
            # 部分 OpenAI 兼容服务流式时不返回 usage：无法判断上下文大小，自动压缩对该模型失效
            log.warning(
                "model reported no input token usage, automatic compaction cannot trigger",
                extra={"session_id": session_id},
            )
            return False
        return tokens > self._threshold

    async def compact(self, session: Session, agent: Agent) -> int:
        """压缩到保留窗口之外没有可压缩的为止（每一批一次模型调用）。返回压缩的步数；为 0 表示没有可压缩的。"""
        compacted = 0
        while True:
            async with self._db.transaction() as s:
                previous = await latest_compaction(s, session.id)
                rows = await rows_after(s, session.id, previous.through_message_id if previous else 0)
            steps = steps_of(rows)
            if len(steps) <= self._keep_steps:
                break
            chosen, more = select_steps(steps, self._keep_steps, _TRANSCRIPT_CHARS)
            summary = await self._runtime.summarize(
                agent,
                COMPACTION_INSTRUCTIONS,
                compaction_prompt(previous.summary if previous else None, chosen),
            )
            through = chosen[-1][-1].id
            async with self._db.transaction() as s:
                s.add(Compaction(session_id=session.id, through_message_id=through, summary=summary))
            compacted += len(chosen)
            log.info(
                "session compacted",
                extra={
                    "session_id": session.id,
                    "steps": len(chosen),
                    "through_message_id": through,
                    "more": more,
                },
            )
            if not more:
                break
        return compacted

    async def request_in(self, s: AsyncSession, session: Session) -> bool:
        """用户手动压缩：在会话的 lane 上投递一次（排在正在进行与排队的运行之后）；已有待执行的不重复投递。"""
        lane = session_lane(session.id)
        pending = await s.scalar(
            select(Task.id)
            .where(Task.lane == lane, Task.kind == TaskKind.COMPACTION, Task.status == TaskStatus.PENDING)
            .limit(1)
        )
        if pending is not None:
            return False
        self._queue.enqueue(
            s,
            kind=TaskKind.COMPACTION,
            lane=lane,
            agent_id=session.agent_id,
            session_id=session.id,
            payload={},
        )
        return True


class CompactionHandler:
    """用户手动触发的压缩（同一个 Compactor.compact）。"""

    def __init__(self, db: Database, compactor: Compactor):
        self._db = db
        self._compactor = compactor

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """失败只需可见（任务记录、日志），没有等着结果的一方。"""

    async def execute(self, task: Task) -> Commit:
        if task.session_id is None:
            raise RuntimeError(f"compaction task {task.id} must reference a session")
        async with self._db.transaction() as s:
            session = await s.get(Session, task.session_id)
            agent = await s.get(Agent, session.agent_id) if session is not None else None
        if session is None or agent is None:
            raise RuntimeError(f"compaction task {task.id} references a missing session or agent")
        if not await self._compactor.compact(session, agent):
            log.info("nothing to compact beyond the kept steps", extra={"session_id": session.id})
        return no_commit
