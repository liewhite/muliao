"""session 上下文压缩，与模型提供方无关。

- 触发：一轮结束时最后一次模型请求的输入 token 超过阈值，在同一事务里投递 compaction task（与 turn 同一 lane，
  同一 session 最多一个待执行）；也可由用户在会话页手动触发。
- 执行：把「最近 keep_turns 轮之前」的消息连同上一份摘要交给该 agent 的模型总结，写入 compactions 表。
  切点总在两轮之间，工具调用与其返回不会被拆开。
- 使用：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息。原始消息不删除。

只在两轮之间压缩：单轮内工具调用导致上下文超限时该轮失败，需要用户手动压缩后重试该轮。
"""

import logging

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


def context_tokens(messages: list[ModelMessage]) -> int:
    """最后一次模型请求的输入 token 数（pydantic-ai 已把缓存读写 token 计入），即本轮结束时上下文的实际大小。"""
    for msg in reversed(messages):
        if isinstance(msg, ModelResponse):
            return msg.usage.input_tokens
    raise ValueError("a completed turn must end with a model response")


class CompactionScheduler:
    """决定何时压缩，并保证同一 session 最多一个待执行的压缩。"""

    def __init__(self, queue: TaskQueue, threshold_tokens: int):
        self._queue = queue
        self._threshold = threshold_tokens

    async def request(self, s: AsyncSession, session_id: str, agent_id: str) -> bool:
        """投递压缩；同 lane 已有待执行的压缩时不重复投递。返回是否新投递了。"""
        pending = await s.scalar(
            select(Task.id)
            .where(
                Task.lane == session_lane(session_id),
                Task.kind == TaskKind.COMPACTION,
                Task.status == TaskStatus.PENDING,
            )
            .limit(1)
        )
        if pending is not None:
            return False
        self._queue.enqueue(
            s,
            kind=TaskKind.COMPACTION,
            lane=session_lane(session_id),
            agent_id=agent_id,
            session_id=session_id,
            payload={},
        )
        return True

    async def after_run(
        self, s: AsyncSession, session: Session, model: str, new_messages: list[ModelMessage]
    ) -> None:
        tokens = context_tokens(new_messages)
        ctx = {"session_id": session.id, "agent": session.agent_id, "model": model, "tokens": tokens}
        if tokens == 0:
            # 部分 OpenAI 兼容服务流式时不返回 usage：无法判断上下文大小，自动压缩对该模型失效
            log.warning("model reported no input token usage, automatic compaction cannot trigger", extra=ctx)
            return
        if tokens > self._threshold and await self.request(s, session.id, session.agent_id):
            log.info("context over threshold, compaction queued", extra=ctx)


def _messages(turn: list[Message]) -> list[ModelMessage]:
    return load_messages(r.payload for r in turn)


def _group_turns(rows: list[Message]) -> list[list[Message]]:
    """按产生它们的 turn task 分组，保持时间顺序。

    切点必须落在所有工具调用都已有结果的地方：一轮停在挂起的调用上（如 ask_user 等用户回答）时，
    它和给出结果的后续轮次合成一组，否则切在中间会让留下的结果找不到对应的调用。
    """
    turns: list[list[Message]] = []
    for row in rows:
        if turns and (turns[-1][-1].task_id == row.task_id or unanswered_calls(_messages(turns[-1]))):
            turns[-1].append(row)
        else:
            turns.append([row])
    return turns


def _turn_text(turn: list[Message]) -> str:
    return to_text(to_items(load_messages(r.payload for r in turn)), max_item_chars=_ITEM_CHARS)


def select_turns(
    turns: list[list[Message]], keep_turns: int, max_chars: int
) -> tuple[list[list[Message]], bool]:
    """从最早的可压缩轮次开始，取总长不超过 max_chars 的若干轮（至少一轮）。

    返回 (本次压缩的轮次, 是否还有可压缩的轮次留给下一次)。不截断内容：超出的轮次下次再压缩。
    """
    compactable = turns[:-keep_turns]
    chosen: list[list[Message]] = []
    total = 0
    for turn in compactable:
        size = len(_turn_text(turn))
        if chosen and total + size > max_chars:
            break
        chosen.append(turn)
        total += size
    return chosen, len(chosen) < len(compactable)


def compaction_prompt(previous_summary: str | None, turns: list[list[Message]]) -> str:
    transcript = "\n".join(_turn_text(t) for t in turns)
    previous = f"<previous_summary>\n{previous_summary}\n</previous_summary>\n\n" if previous_summary else ""
    return f"{previous}<conversation>\n{transcript}\n</conversation>\n\nWrite the updated summary."


class CompactionHandler:
    def __init__(self, db: Database, runtime: AgentRuntime, scheduler: CompactionScheduler, keep_turns: int):
        self._db = db
        self._runtime = runtime
        self._scheduler = scheduler
        self._keep_turns = keep_turns

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """失败只需可见（任务记录、日志），没有等着结果的一方。"""

    async def execute(self, task: Task) -> Commit:
        session_id = task.session_id
        if session_id is None or task.agent_id is None:
            raise RuntimeError(f"compaction task {task.id} must reference a session and an agent")
        async with self._db.transaction() as s:
            agent = await s.get(Agent, task.agent_id)
            if agent is None:
                raise RuntimeError(f"compaction task {task.id} references missing agent {task.agent_id!r}")
            previous = await latest_compaction(s, session_id)
            rows = await rows_after(s, session_id, previous.through_message_id if previous else 0)
        turns = _group_turns(rows)
        ctx = {"session_id": session_id, "turns": len(turns)}

        if len(turns) <= self._keep_turns:
            # 保留窗口外没有可压缩的轮次（例如之前的压缩已经处理过）
            log.info("nothing to compact beyond the kept turns", extra=ctx)
            return no_commit

        chosen, more = select_turns(turns, self._keep_turns, _TRANSCRIPT_CHARS)
        summary = await self._runtime.summarize(
            agent, COMPACTION_INSTRUCTIONS, compaction_prompt(previous.summary if previous else None, chosen)
        )
        through = chosen[-1][-1].id
        log.info(
            "session compacted",
            extra={**ctx, "compacted_turns": len(chosen), "through_message_id": through, "more": more},
        )

        async def commit(s: AsyncSession) -> None:
            s.add(Compaction(session_id=session_id, through_message_id=through, summary=summary))
            if more:
                # 超出单次上限的轮次留给下一次压缩，直到收敛
                await self._scheduler.request(s, session_id, agent.id)

        return commit
