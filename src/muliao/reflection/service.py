"""强制反思：agent 自上次反思以来做了足够多步（模型回复数达到 reflect_every），就把这段区间的会话记录交给它，
审视是否需要更新 skill、memory 与自己的指令。

- 调度（ReflectionScheduler）：每次运行结束时数一次，步数由 messages 表推导（检查点之后该 agent 的模型回复条数），
  不另存计数。达到阈值就投递一个 reflection task（载荷是消息 id 区间），检查点推进到当前最大消息 id。
- 执行（ReflectionHandler）。
"""

import logging
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.runner import AgentRuntime, ToolGroups
from muliao.agents.events import AgentDeleting
from muliao.agents.service import AgentService
from muliao.agents.tables import Agent
from muliao.agents.tool_catalog import REFLECTION_GROUPS
from muliao.errors import NotFound
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task, TaskKind
from muliao.platform.tasks import Commit, TaskQueue, no_commit, reflection_lane
from muliao.reflection.tables import Reflection, ReflectionCheckpoint
from muliao.sessions.codec import KIND_PATH, RESPONSE_KIND, load_messages
from muliao.sessions.events import RunEnded
from muliao.sessions.tables import Message, Session
from muliao.sessions.transcript import to_items, to_text

log = logging.getLogger(__name__)

_ITEM_CHARS = 2000
_TRANSCRIPT_CHARS = 120_000

REFLECTION_PROMPT = """\
This is a scheduled self-review, not a user request. Below is the transcript of your recent work.

Review it and decide:
1. Memory — durable facts worth keeping (user preferences, environment details, corrections the user made, \
lessons learned), or existing entries that are now wrong, outdated or redundant. Use the memory tools.
2. Skills — a multi-step procedure you worked out that is likely to recur, a better way of doing something \
a skill describes, or a skill that proved wrong or incomplete. Use the skill tools.
3. Instructions — a standing rule in your own instructions that proved wrong, or a rule the user keeps \
having to repeat. Use `instructions_replace` sparingly: small, precise edits only.

Do not record one-off details or anything already captured. If nothing needs to change, change nothing.
Finish with a short summary of exactly what you changed and why, or "No changes" with a one-line reason.

<transcript>
{transcript}
</transcript>"""


def render_transcript(sessions: dict[str, Session], rows: list[Message]) -> str:
    # rows 按 id 排序；按 session 首次出现的先后分组
    by_session: dict[str, list[Message]] = {}
    for row in rows:
        by_session.setdefault(row.session_id, []).append(row)
    blocks = []
    for session_id, group in by_session.items():
        session = sessions[session_id]
        items = to_items(load_messages(r.payload for r in group))
        header = f"### Session: {session.title} (origin: {session.origin})"
        blocks.append(f"{header}\n{to_text(items, max_item_chars=_ITEM_CHARS)}")
    text = "\n\n".join(blocks)
    # 超长时保留最近的部分
    return (
        text
        if len(text) <= _TRANSCRIPT_CHARS
        else "…[earlier messages omitted]\n" + text[-_TRANSCRIPT_CHARS:]
    )


async def list_reflections_of(db: Database, agent_id: str, limit: int) -> list[Reflection]:
    async with db.transaction() as s:
        query = (
            select(Reflection)
            .where(Reflection.agent_id == agent_id)
            .order_by(Reflection.id.desc())
            .limit(limit)
        )
        return list((await s.scalars(query)).all())


def _steps_after(agent_id: str, checkpoint: int):  # noqa: ANN202  返回 SQL 查询
    """该 agent 在检查点之后的模型回复（每条回复就是一步）。"""
    return (
        select(func.count(Message.id))
        .join(Session, Session.id == Message.session_id)
        .where(
            Session.agent_id == agent_id,
            Message.id > checkpoint,
            func.json_extract(Message.payload, KIND_PATH) == RESPONSE_KIND,
        )
    )


class ReflectionScheduler:
    """反思的调度（反思领域）：订阅 RunEnded，按该 agent 自检查点以来的步数决定是否投递反思；检查点在自己的表里。"""

    def __init__(self, db: Database, queue: TaskQueue, agents: AgentService):
        self._db = db
        self._queue = queue
        self._agents = agents

    def subscribe(self, messaging: Messaging) -> None:
        # 同一个 agent 的两种事件在同一条 lane 上按发布顺序处理：RunEnded 一定先于 AgentDeleting 发布（agent 有会话忙时
        # 不能删除），删除的清理之后不会再写入检查点、投递反思
        messaging.subscribe(RunEnded, "reflection.run_ended", self.on_run_ended, lane=_agent_lane)
        messaging.subscribe(AgentDeleting, "reflection.agent_deleting", self.on_agent_deleting, lane=_agent_lane)

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：排队的反思不再执行，反思记录与检查点删除（正在执行的那次由 agent 领域等它结束）。"""

        async def commit(s: AsyncSession) -> None:
            await self._queue.finish_pending_in(s, reflection_lane(event.agent_id), TaskKind.REFLECTION)
            await s.execute(delete(Reflection).where(Reflection.agent_id == event.agent_id))
            await s.execute(
                delete(ReflectionCheckpoint).where(ReflectionCheckpoint.agent_id == event.agent_id)
            )

        return commit

    async def on_run_ended(self, event: RunEnded, _delivery: Delivery) -> Commit:
        """步数达到该 agent 的 reflect_every 就投递反思，检查点推进到当前最大消息 id。reflect_every 是 agent 领域的配置：
        在自己的只读事务里查（docs/domain-design.md 1.1），改动与这次判断之间的先后无关紧要。"""
        try:
            agent = await self._agents.get(event.agent_id)
        except NotFound:
            log.info("run of a deleted agent not counted for reflection", extra={"agent": event.agent_id})
            return no_commit
        if agent.deleting_by is not None:
            log.info("run of a deleting agent not counted for reflection", extra={"agent": event.agent_id})
            return no_commit
        reflect_every = agent.reflect_every

        async def commit(s: AsyncSession) -> None:
            checkpoint = await _checkpoint_in(s, event.agent_id)
            if await s.scalar(_steps_after(event.agent_id, checkpoint.message_id)) < reflect_every:
                return
            to_id = await s.scalar(
                select(func.max(Message.id))
                .join(Session, Session.id == Message.session_id)
                .where(Session.agent_id == event.agent_id)
            )
            if to_id is None:
                raise RuntimeError(f"agent {event.agent_id} has steps to reflect on but no messages")
            self._queue.enqueue(
                s,
                kind=TaskKind.REFLECTION,
                lane=reflection_lane(event.agent_id),
                agent_id=event.agent_id,
                session_id=None,
                payload={"from_message_id": checkpoint.message_id, "to_message_id": to_id},
            )
            log.info("reflection scheduled", extra={"agent": event.agent_id, "to_message_id": to_id})
            checkpoint.message_id = to_id

        return commit

    async def steps_since(self, agents: Sequence[Agent]) -> dict[str, int]:
        """这些 agent 自上次反思以来的步数（界面显示「距下次反思」）。"""
        async with self._db.transaction() as s:
            result = {}
            for a in agents:
                checkpoint = await s.get(ReflectionCheckpoint, a.id)
                result[a.id] = (
                    await s.scalar(_steps_after(a.id, checkpoint.message_id if checkpoint else 0)) or 0
                )
            return result


def _agent_lane(event: RunEnded | AgentDeleting) -> str:
    return f"reflection-events:{event.agent_id}"


async def _checkpoint_in(s: AsyncSession, agent_id: str) -> ReflectionCheckpoint:
    checkpoint = await s.get(ReflectionCheckpoint, agent_id)
    if checkpoint is None:
        checkpoint = ReflectionCheckpoint(agent_id=agent_id, message_id=0)
        s.add(checkpoint)
    return checkpoint


class ReflectionHandler:
    def __init__(self, db: Database, runtime: AgentRuntime, tool_groups: ToolGroups):
        self._db = db
        self._runtime = runtime
        self._tool_groups = tool_groups

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """失败只需可见（任务记录、日志），没有等着结果的一方。"""

    async def execute(self, task: Task) -> Commit:
        from_id, to_id = task.payload["from_message_id"], task.payload["to_message_id"]
        async with self._db.transaction() as s:
            agent = await s.get(Agent, task.agent_id)
            if agent is None:
                raise RuntimeError(f"reflection task {task.id} references missing agent {task.agent_id!r}")
            rows = list(
                (
                    await s.scalars(
                        select(Message)
                        .join(Session, Session.id == Message.session_id)
                        .where(Session.agent_id == agent.id, Message.id > from_id, Message.id <= to_id)
                        .order_by(Message.id)
                    )
                ).all()
            )
            session_ids = {r.session_id for r in rows}
            sessions = {
                x.id: x for x in (await s.scalars(select(Session).where(Session.id.in_(session_ids)))).all()
            }

        if rows:
            prompt = REFLECTION_PROMPT.format(transcript=render_transcript(sessions, rows))
            scope = self._runtime.scope(agent, session_id=None, origin=None, audience=None)
            # 反思读的转录里可能有不可信输入：只开放自我改进，以及该 agent 本来就有的写公共 skill 的权限
            profile = self._tool_groups.profile(agent.id, agent.tool_groups, within=REFLECTION_GROUPS)
            result = await self._runtime.run_once(agent, scope, prompt, profile)
            output, messages = result.output, result.all_messages_json().decode()
        else:
            # 不浪费一次模型调用，如实记录是哪种情况：区间本来就是空的，还是区间里的消息随会话一起被删除了
            reason = (
                "no new messages since the last reflection"
                if to_id <= from_id
                else "its sessions were deleted"
            )
            output, messages = f"Skipped: nothing to reflect on in this window ({reason}).", "[]"

        async def commit(s: AsyncSession) -> None:
            current = await s.get(Agent, agent.id)
            if current is None or current.deleting_by is not None:
                # 反思期间 agent 进入删除流程：它的反思记录已被清理，这条不再写
                log.info("reflection of a deleted agent not recorded", extra={"agent": agent.id})
                return
            s.add(
                Reflection(
                    agent_id=agent.id,
                    from_message_id=from_id,
                    to_message_id=to_id,
                    output=output,
                    messages=messages,
                )
            )

        return commit
