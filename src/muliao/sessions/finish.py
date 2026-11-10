"""一次运行的收尾（与运行记为完成在同一个事务里），只写会话自己的数据：更新时间、委派的结清、唤醒下一次运行，并发布
RunEnded（通知领域订阅它做推送，issue 看板订阅它请 agent 表态）。
运行被用户停止时的委派结局与信箱由停止处理（sessions.halt），这里只做其余部分。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import shared_attachments
from muliao.platform.db import utcnow
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import Commit, TaskQueue
from muliao.sessions.events import RunEnded as RunEndedEvent
from muliao.sessions.holds import has_active_in
from muliao.sessions.loading import load_session
from muliao.sessions.mailbox import queued_in, wake_in
from muliao.sessions.settlement import settle_finished_in
from muliao.sessions.steps import RunState


@dataclass(frozen=True)
class RunEnd:
    """一次运行怎么结束的。"""

    # 给用户的回答：停在提问、等批准上时是问题与等批准的调用，停在等 agent 上时为空
    answer: str
    # 停在挂起的调用上（等用户回答提问、批准，或等被问的 agent）：工作还没做完
    paused: bool
    # 停下来等用户回答提问：要告诉用户
    asked_user: bool
    stopped: bool
    # 停下来等用户批准的调用 id：不论有没有人在等都要告诉用户
    approvals: tuple[str, ...] = ()
    # 这次运行新请求批准的调用 id -> 工具定下的 pin：和历史的结尾一起记下（Steps.save_end，sessions.approvals）。从历史收尾时为空：那些调用
    # 正是按已记下的记录认出来的，pin 已在库里
    requested: Mapping[str, str] = field(default_factory=dict)


class RunFinisher:
    def __init__(
        self,
        queue: TaskQueue,
        messaging: Messaging,
    ):
        self._queue = queue
        self._messaging = messaging

    async def _work_done(self, s: AsyncSession, session_id: str) -> bool:
        """会话的工作做完了：没有人替它在外面做事（没有 active 占用），信箱里也没有还要交给模型的信件（比如在最后
        一次请求期间才到的后台任务结局，它会唤醒下一次运行，结清由那次运行做）。只看会话自己的数据。"""
        return not await has_active_in(s, session_id) and not await queued_in(s, session_id)

    def idle(self, state: RunState) -> Commit:
        """运行没有可交给模型的、也没有之前的尝试要收尾（信箱空了，或还在等挂起调用的回答）：只看要不要唤醒下一次。"""
        return self.wake(state.session.id, state.run_id)

    def wake(self, session_id: str, run_id: int) -> Commit:
        """运行结束、没有别的要收尾：只看要不要唤醒下一次。"""

        async def finish(s: AsyncSession) -> None:
            session, _agent = await load_session(s, session_id)
            await wake_in(s, self._queue, session, finished=run_id)

        return finish

    def commit(self, state: RunState, end: RunEnd) -> Commit:
        """收尾：结清、推送与唤醒，和运行记为完成一起提交。历史的结尾与等批准的记录此前已在运行里一起提交（Steps.save_end）。"""

        async def finish(s: AsyncSession) -> None:
            session, agent = await load_session(s, state.session.id)
            if state.messages:
                session.updated_at = utcnow()
                # 推送由通知领域订阅（运行结束后随即投递）
                self._messaging.publish_in(
                    s,
                    RunEndedEvent(
                        session_id=session.id,
                        session_title=session.title,
                        origin=session.origin,
                        origin_ref=session.origin_ref,
                        agent_id=agent.id,
                        agent_name=agent.name,
                        run_id=state.run_id,
                        answer=end.answer,
                        stopped=end.stopped,
                        paused=end.paused,
                        asked_user=end.asked_user,
                        needs_approval=bool(end.approvals),
                        awaited=any(m.awaited for m in state.handled),
                    ),
                )
            if not end.stopped and not end.paused and await self._work_done(s, session.id):
                await settle_finished_in(
                    s, self._queue, session, end.answer, shared_attachments(state.messages)
                )
            await wake_in(s, self._queue, session, finished=state.run_id)

        return finish
