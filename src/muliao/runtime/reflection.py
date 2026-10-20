"""ReflectionHandler：强制反思。把一段区间内的 turn 记录交给 agent，审视是否需要更新 skill 与 memory。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.models import Agent, Message, Reflection, Session, Task
from muliao.runtime.codec import load_messages
from muliao.runtime.runner import AgentRuntime, ToolGroups
from muliao.runtime.tasks import Commit
from muliao.transcript import to_items, to_text

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
        text if len(text) <= _TRANSCRIPT_CHARS else "…[earlier turns omitted]\n" + text[-_TRANSCRIPT_CHARS:]
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
            result = await self._runtime.run(agent, scope, prompt, [], profile)
            output, messages = result.output, result.all_messages_json().decode()
        else:
            # 不浪费一次模型调用，如实记录是哪种情况：区间本来就是空的（如被停止、没有产生消息的轮次凑满了阈值），
            # 还是区间里的消息随会话一起被删除了
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
