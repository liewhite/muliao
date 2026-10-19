"""session 上下文压缩，与模型提供方无关。

- 触发：一轮结束时最后一次模型请求的输入 token 超过阈值，在同一事务里投递 compaction task（与 turn 同一 lane）。
- 执行：把「最近 keep_turns 轮之前」的消息连同上一份摘要交给该 agent 的模型总结，写入 compactions 表。
  切点总在两轮之间，工具调用与其返回不会被拆开。
- 使用：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息。原始消息不删除。

只在两轮之间压缩：单轮内工具调用导致的上下文增长不在覆盖范围内。
"""

import logging
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage, ModelResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.models import Agent, Compaction, Message, Session, Task, TaskKind
from muliao.runtime.codec import load_messages
from muliao.runtime.runner import AgentRuntime
from muliao.runtime.tasks import Commit, TaskQueue, no_commit, turn_lane
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


@dataclass(frozen=True)
class SessionContext:
    """发给模型的 session 上下文。"""

    summary: str | None
    messages: list[ModelMessage]


async def latest_compaction(s: AsyncSession, session_id: str) -> Compaction | None:
    return await s.scalar(
        select(Compaction).where(Compaction.session_id == session_id).order_by(Compaction.id.desc()).limit(1)
    )


async def _rows_after(s: AsyncSession, session_id: str, after_id: int) -> list[Message]:
    query = (
        select(Message).where(Message.session_id == session_id, Message.id > after_id).order_by(Message.id)
    )
    return list((await s.scalars(query)).all())


async def load_context(s: AsyncSession, session_id: str) -> SessionContext:
    compaction = await latest_compaction(s, session_id)
    rows = await _rows_after(s, session_id, compaction.through_message_id if compaction else 0)
    return SessionContext(
        summary=compaction.summary if compaction else None,
        messages=load_messages(r.payload for r in rows),
    )


def context_tokens(messages: list[ModelMessage]) -> int:
    """最后一次模型请求的输入 token 数，即本轮结束时上下文的实际大小。"""
    for msg in reversed(messages):
        if isinstance(msg, ModelResponse):
            return msg.usage.input_tokens
    return 0


class CompactionTrigger:
    def __init__(self, queue: TaskQueue, threshold_tokens: int):
        self._queue = queue
        self._threshold = threshold_tokens

    def maybe_enqueue(self, s: AsyncSession, session: Session, new_messages: list[ModelMessage]) -> None:
        tokens = context_tokens(new_messages)
        if tokens <= self._threshold:
            return
        log.info(
            "context over threshold, compaction queued", extra={"session_id": session.id, "tokens": tokens}
        )
        self._queue.enqueue(
            s,
            kind=TaskKind.COMPACTION,
            lane=turn_lane(session.id),
            agent_name=session.agent_name,
            session_id=session.id,
            payload={},
        )


def _group_turns(rows: list[Message]) -> list[list[Message]]:
    """按产生它们的 turn task 分组，保持时间顺序。"""
    turns: list[list[Message]] = []
    for row in rows:
        if turns and turns[-1][0].task_id == row.task_id:
            turns[-1].append(row)
        else:
            turns.append([row])
    return turns


def compaction_prompt(previous_summary: str | None, rows: list[Message]) -> str:
    transcript = to_text(to_items(load_messages(r.payload for r in rows)), max_item_chars=_ITEM_CHARS)
    if len(transcript) > _TRANSCRIPT_CHARS:
        transcript = "…[earliest part omitted]\n" + transcript[-_TRANSCRIPT_CHARS:]
    previous = f"<previous_summary>\n{previous_summary}\n</previous_summary>\n\n" if previous_summary else ""
    return f"{previous}<conversation>\n{transcript}\n</conversation>\n\nWrite the updated summary."


class CompactionHandler:
    def __init__(self, db: Database, runtime: AgentRuntime, keep_turns: int):
        self._db = db
        self._runtime = runtime
        self._keep_turns = keep_turns

    async def execute(self, task: Task) -> Commit:
        assert task.session_id is not None
        async with self._db.transaction() as s:
            agent = await s.get(Agent, task.agent_name)
            if agent is None:
                raise RuntimeError(f"compaction task {task.id} references missing agent {task.agent_name!r}")
            previous = await latest_compaction(s, task.session_id)
            rows = await _rows_after(s, task.session_id, previous.through_message_id if previous else 0)
        turns = _group_turns(rows)
        ctx = {"session_id": task.session_id, "turns": len(turns)}

        if len(turns) <= self._keep_turns:
            # 之前排队的压缩已经处理过这段内容，或保留窗口外没有可压缩的轮次
            log.info("nothing to compact beyond the kept turns", extra=ctx)
            return no_commit

        to_compact = [row for turn in turns[: -self._keep_turns] for row in turn]
        summary = await self._runtime.summarize(
            agent,
            COMPACTION_INSTRUCTIONS,
            compaction_prompt(previous.summary if previous else None, to_compact),
        )
        through = to_compact[-1].id
        log.info(
            "session compacted", extra={**ctx, "through_message_id": through, "summary_chars": len(summary)}
        )

        async def commit(s: AsyncSession) -> None:
            s.add(Compaction(session_id=task.session_id, through_message_id=through, summary=summary))

        return commit
