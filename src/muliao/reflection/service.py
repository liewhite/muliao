"""强制反思：agent 自上次反思以来做了足够多步（模型回复数达到 reflect_every），就把这段区间的会话记录交给它，
审视是否需要更新 skill、memory 与自己的指令。

- 调度（ReflectionScheduler）：每次运行结束时数一次，步数由 messages 表推导（检查点之后该 agent 的模型回复条数），
  不另存计数。达到阈值就投递一个 reflection task（载荷是消息 id 区间），检查点推进到当前最大消息 id。
- 执行（ReflectionHandler）。
"""

import logging
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import AgentRuntime, ToolGroups
from muliao.agents.api import REFLECTION_GROUPS, AgentInfo, AgentService, active_agent_in
from muliao.agents.events import AgentDeleting
from muliao.errors import NotFound
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, TaskQueue, no_commit
from muliao.reflection.tables import Reflection, ReflectionCheckpoint
from muliao.sessions.api import AgentHistory
from muliao.sessions.events import RunEnded

log = logging.getLogger(__name__)

# 反思的任务种类（组合根为它登记 ReflectionHandler）
REFLECTION_TASK = "reflection"

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


def bounded(transcript: str) -> str:
    """超长时保留最近的部分。"""
    return (
        transcript
        if len(transcript) <= _TRANSCRIPT_CHARS
        else "…[earlier messages omitted]\n" + transcript[-_TRANSCRIPT_CHARS:]
    )


class ReflectionLog:
    """反思的只读一面（HTTP 层用）：反思记录与距下次反思的步数。调度（ReflectionScheduler）的订阅、投递与删除检查
    不在这里，HTTP 层拿不到。"""

    def __init__(self, db: Database, history: AgentHistory):
        self._db = db
        self._history = history

    async def list(self, agent_id: str, limit: int) -> list[Reflection]:
        """这个 agent 最近的反思记录，新的在前。"""
        async with self._db.transaction() as s:
            query = (
                select(Reflection)
                .where(Reflection.agent_id == agent_id)
                .order_by(Reflection.id.desc())
                .limit(limit)
            )
            return list((await s.scalars(query)).all())

    async def steps_since(self, agents: Sequence[AgentInfo]) -> dict[str, int]:
        """这些 agent 自上次反思以来的步数（界面显示「距下次反思」）。"""
        async with self._db.transaction() as s:
            checkpoints = {
                c.agent_id: c.message_id
                for c in (
                    await s.scalars(
                        select(ReflectionCheckpoint).where(
                            ReflectionCheckpoint.agent_id.in_([a.id for a in agents])
                        )
                    )
                ).all()
            }
        return {a.id: await self._history.steps_after(a.id, checkpoints.get(a.id, 0)) for a in agents}


class ReflectionScheduler:
    """反思的调度（反思领域）：订阅 RunEnded，按该 agent 自检查点以来的步数决定是否投递反思；检查点在自己的表里。"""

    def __init__(self, db: Database, queue: TaskQueue, agents: AgentService, history: AgentHistory):
        self._db = db
        self._queue = queue
        self._agents = agents
        self._history = history

    def subscribe(self, messaging: Messaging) -> None:
        # 同一个 agent 的两种事件在同一条 lane 上按发布顺序处理。通常 RunEnded 先于 AgentDeleting 发布（agent 有会话忙时
        # 不能删除）；排在 AgentDeleting 之后的 RunEnded（删除前检查与标记之间排上的运行、人工重试的投递）由 on_run_ended
        # 检查 agent 已在删除而跳过——这个检查是必需的，不是防御。两者合起来，删除的清理之后不会再写检查点、投递反思
        messaging.subscribe(RunEnded, "reflection.run_ended", self.on_run_ended, lane=_agent_lane)
        messaging.subscribe(
            AgentDeleting, "reflection.agent_deleting", self.on_agent_deleting, lane=_agent_lane
        )

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：排队的反思不再执行，反思记录与检查点删除（正在执行的那次由 agent 领域等它结束）。"""

        async def commit(s: AsyncSession) -> None:
            await self._queue.finish_pending_in(s, reflection_lane(event.agent_id), REFLECTION_TASK)
            await s.execute(delete(Reflection).where(Reflection.agent_id == event.agent_id))
            await s.execute(
                delete(ReflectionCheckpoint).where(ReflectionCheckpoint.agent_id == event.agent_id)
            )

        return commit

    async def on_run_ended(self, event: RunEnded, _delivery: Delivery) -> Commit:
        """步数达到该 agent 的 reflect_every 就投递反思，检查点推进到当前最大消息 id。reflect_every 与步数是别的领域的
        数据，在它们自己的只读事务里查（docs/architecture.md 2.2）：配置改动与这次判断的先后无关紧要；消息只会追加，
        数到的步数之后又多了几步，下一次运行结束时照样数到。检查点只由这条 lane 写（单写者），先读后写没有竞态。"""
        try:
            # 标记删除先于 AgentDeleting 的处理：排在它之后的 RunEnded 在这里一定看到 agent 已在删除
            agent = await self._agents.get_active(event.agent_id)
        except NotFound:
            log.info("run of a deleted agent not counted for reflection", extra={"agent": event.agent_id})
            return no_commit
        async with self._db.transaction() as s:
            checkpoint = await s.get(ReflectionCheckpoint, event.agent_id)
            from_id = checkpoint.message_id if checkpoint is not None else 0
        # 步数与区间上界取自同一个快照：有步数就一定有更晚的消息
        steps, to_id = await self._history.window_after(event.agent_id, from_id)
        if steps < agent.reflect_every:
            return no_commit
        if to_id is None or to_id <= from_id:
            raise RuntimeError(
                f"agent {event.agent_id} has {steps} steps after message {from_id} but its last message is {to_id}"
            )

        async def commit(s: AsyncSession) -> None:
            checkpoint = await _checkpoint_in(s, event.agent_id)
            if checkpoint.message_id != from_id:
                raise RuntimeError(
                    f"reflection checkpoint of agent {event.agent_id} moved from {from_id} to "
                    f"{checkpoint.message_id} outside its lane"
                )
            self._queue.enqueue(
                s,
                kind=REFLECTION_TASK,
                lane=reflection_lane(event.agent_id),
                agent_id=event.agent_id,
                session_id=None,
                payload={"from_message_id": from_id, "to_message_id": to_id},
            )
            log.info("reflection scheduled", extra={"agent": event.agent_id, "to_message_id": to_id})
            checkpoint.message_id = to_id

        return commit

    async def blocks_agent_deletion_in(self, s: AsyncSession, agent_id: str) -> bool:
        """这个 agent 的反思在排队或执行（删除 agent 的检查，组合根注入 AgentLifecycle）：执行中的反思会写它的资源。"""
        return await self._queue.lane_busy(s, reflection_lane(agent_id))


def reflection_lane(agent_id: str) -> str:
    """一个 agent 的反思任务：同一时刻只有一次反思在写它的资源。"""
    return f"reflection:{agent_id}"


def _agent_lane(event: RunEnded | AgentDeleting) -> str:
    return f"reflection-events:{event.agent_id}"


async def _checkpoint_in(s: AsyncSession, agent_id: str) -> ReflectionCheckpoint:
    checkpoint = await s.get(ReflectionCheckpoint, agent_id)
    if checkpoint is None:
        checkpoint = ReflectionCheckpoint(agent_id=agent_id, message_id=0)
        s.add(checkpoint)
    return checkpoint


class ReflectionHandler:
    def __init__(
        self,
        db: Database,
        runtime: AgentRuntime,
        tool_groups: ToolGroups,
        agents: AgentService,
        history: AgentHistory,
    ):
        self._db = db
        self._runtime = runtime
        self._tool_groups = tool_groups
        self._agents = agents
        self._history = history

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """失败只需可见（任务记录、日志），没有等着结果的一方。"""

    async def execute(self, task: Task) -> Commit:
        if task.agent_id is None:
            raise RuntimeError(f"reflection task {task.id} must reference an agent")
        from_id, to_id = task.payload["from_message_id"], task.payload["to_message_id"]
        # 反思的 lane 忙时 agent 的行不会被删（删除流程的最后一步等这条 lane 空闲）：行不在是 bug，agent_in 报错
        await self._agents.get(task.agent_id)
        try:
            agent = await self._agents.get_active(task.agent_id)
        except NotFound:
            # 投递之后 agent 进入删除流程：删除流程会结束排队的反思，这一次（已开始）不做
            log.info("reflection of a deleting agent skipped", extra={"agent": task.agent_id})
            return no_commit
        transcript = await self._history.transcript(agent.id, from_id, to_id, max_item_chars=_ITEM_CHARS)
        if transcript:
            prompt = REFLECTION_PROMPT.format(transcript=bounded(transcript))
            scope = self._runtime.scope(agent, session_id=None, audience=None)
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
            try:
                # agent 只会从可用变为删除（单向），在本事务里检查没有竞态（docs/architecture.md 2.2）
                await active_agent_in(s, agent.id)
            except NotFound:
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
