"""一次运行的收尾（与运行记为完成在同一个事务里）：会话的已读状态、反思调度、委派的结局、推送、唤醒下一次运行。
运行被用户停止时的委派结局与信箱由停止处理（runtime.halt），这里只做其余部分。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.conventions import shared_attachments
from muliao.db import utcnow
from muliao.delivery.base import Notifier
from muliao.models import Agent, Notification, Session
from muliao.runtime.delegations import Ending, Outcome, in_session_in, settle_all_in
from muliao.runtime.mailbox import wake_in
from muliao.runtime.reflection import ReflectionScheduler
from muliao.runtime.steps import RunState
from muliao.runtime.tasks import Commit, TaskQueue
from muliao.runtime.work import SessionWork, any_active_in


@dataclass(frozen=True)
class RunEnd:
    """一次运行怎么结束的。"""

    # 给用户的回答：停在提问上时是问题列表，停在等 agent 上时为空
    answer: str
    # 停在挂起的调用上（等用户回答提问，或等被问的 agent）：工作还没做完
    paused: bool
    # 停下来等用户回答提问：要告诉用户
    asked_user: bool
    stopped: bool


async def load_session(s: AsyncSession, session_id: str) -> tuple[Session, Agent]:
    session = await s.get(Session, session_id)
    if session is None:
        raise RuntimeError(f"session {session_id} vanished during its run")
    agent = await s.get(Agent, session.agent_id)
    if agent is None:
        raise RuntimeError(f"session {session_id} references missing agent {session.agent_id}")
    return session, agent


class RunFinisher:
    def __init__(
        self,
        queue: TaskQueue,
        notifier: Notifier,
        reflection: ReflectionScheduler,
        works: Sequence[SessionWork],
    ):
        self._queue = queue
        self._notifier = notifier
        self._reflection = reflection
        self._works = works

    def idle(self, state: RunState) -> Commit:
        """运行没有可交给模型的、也没有之前的尝试要收尾（信箱空了，或还在等挂起调用的回答）：只看要不要唤醒下一次。"""

        async def finish(s: AsyncSession) -> None:
            session, _agent = await load_session(s, state.session.id)
            await wake_in(s, self._queue, session, finished=state.run_id)

        return finish

    def commit(self, state: RunState, end: RunEnd) -> Commit:
        async def finish(s: AsyncSession) -> None:
            session, agent = await load_session(s, state.session.id)
            if state.messages:
                session.updated_at = utcnow()
                session.unread = not session.private
                await self._reflection.after_run_in(s, agent)
            if not end.stopped and not end.paused and not await any_active_in(self._works, s, session.id):
                # 工作做完了（没有停在挂起的调用上，它发起的异步工作也都结束了）：处理中的委派交回结局。委派可能跨几次
                # 运行：停在挂起的调用上、或等它发起的工作时，由之后那次做完的运行交回
                outcome = Outcome(Ending.FINISHED, end.answer, shared_attachments(state.messages))
                await settle_all_in(
                    s, self._queue, await in_session_in(s, session.id, consumed_only=True), session, outcome
                )
            if not end.stopped and state.messages:
                # 推送：处理过的信件里有用户在等的，且有话要告诉用户（做完了，或停下来问用户）
                if any(m.awaited for m in state.handled) and (not end.paused or end.asked_user):
                    await self._notifier.enqueue_in(
                        s,
                        agent.id,
                        Notification(
                            title=f"{agent.name} · {session.title}", body=end.answer, session_id=session.id
                        ),
                    )
            await wake_in(s, self._queue, session, finished=state.run_id)

        return finish
