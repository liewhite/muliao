"""强制反思：agent 自上次反思以来做了足够多步（模型回复数达到 reflect_every），就把这段区间的会话记录交给它，
审视是否需要更新 skill、memory 与自己的指令。

- 调度（ReflectionScheduler）：每次运行结束时数一次，步数由 messages 表推导（检查点之后该 agent 的模型回复条数），
  不另存计数。达到阈值就投递一个 reflection task（载荷是消息 id 区间），检查点推进到当前最大消息 id。
- 执行（ReflectionHandler）。
"""

import logging
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.models import Agent, Message, Reflection, Session, Task, TaskKind
from muliao.runtime.codec import KIND_PATH, RESPONSE_KIND, load_messages
from muliao.runtime.runner import AgentRuntime, ToolGroups
from muliao.runtime.tasks import Commit, TaskQueue, reflection_lane
from muliao.transcript import to_items, to_text

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


# 反思能用到的可选工具组（自我改进之外）
REFLECTION_GROUPS = frozenset({"public_skills"})


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
    def __init__(self, db: Database, queue: TaskQueue):
        self._db = db
        self._queue = queue

    async def steps_since(self, agents: Sequence[Agent]) -> dict[str, int]:
        """这些 agent 自上次反思以来的步数（界面显示「距下次反思」）。"""
        async with self._db.transaction() as s:
            return {a.id: await self.steps_since_in(s, a) for a in agents}

    async def steps_since_in(self, s: AsyncSession, agent: Agent) -> int:
        return await s.scalar(_steps_after(agent.id, agent.reflection_checkpoint)) or 0

    async def after_run_in(self, s: AsyncSession, agent: Agent) -> None:
        """运行结束时（它的收尾事务里）：步数达到阈值就投递反思，检查点推进到当前最大消息 id。"""
        if await self.steps_since_in(s, agent) < agent.reflect_every:
            return
        to_id = await s.scalar(
            select(func.max(Message.id))
            .join(Session, Session.id == Message.session_id)
            .where(Session.agent_id == agent.id)
        )
        if to_id is None:
            raise RuntimeError(f"agent {agent.id} has steps to reflect on but no messages")
        self._queue.enqueue(
            s,
            kind=TaskKind.REFLECTION,
            lane=reflection_lane(agent.id),
            agent_id=agent.id,
            session_id=None,
            payload={"from_message_id": agent.reflection_checkpoint, "to_message_id": to_id},
        )
        log.info("reflection scheduled", extra={"agent": agent.id, "to_message_id": to_id})
        agent.reflection_checkpoint = to_id


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
            scope = self._runtime.scope(agent, session_id=None, origin=None, reply_to=None)
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
