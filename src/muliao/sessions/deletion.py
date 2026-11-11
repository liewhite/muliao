"""删除会话的唯一规则与删除 agent 流程里会话领域的一步（docs/architecture.md 第 7 节「thread 与结果路由」）。"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import TaskQueue
from muliao.sessions.activity import blocking_deletion_in
from muliao.sessions.directory import of_agent_in
from muliao.sessions.events import SessionDeleted
from muliao.sessions.settlement import settle_deleted_in
from muliao.sessions.tables import Session

log = logging.getLogger(__name__)


async def delete_sessions(
    s: AsyncSession, queue: TaskQueue, messaging: Messaging, roots: list[Session]
) -> None:
    """删除会话的唯一规则（删会话、删 agent 都经过这里）。

    它开出的 thread 保留为独立会话（parent 置空）。
    任一被删会话忙时拒绝（判断只看会话自己的数据），避免运行结果无处提交、工具写到被删的目录。交给被删会话、
    还没结清的委派结清为「被删除」，结局交回发起方（sessions.delegation）。
    """
    doomed = {x.id: x for x in roots}
    for session_id in await blocking_deletion_in(s, queue, doomed):
        # 错误也会交还给模型，不带 id；定位信息记在日志里
        log.info("session deletion refused: busy", extra={"session_id": session_id})
        raise Conflict(
            "the session is busy (a run is queued or running, or background jobs or delegated work are still "
            "going); stop it first, then delete"
        )
    for session in doomed.values():
        # 交给被删会话、还没结清的工作交回「被删除」
        await settle_deleted_in(s, queue, session)
    for session in doomed.values():
        await s.delete(session)
    # 立即执行删除：同一事务里随后的查询（订阅者、删除 agent 的流程）看到的是删除之后的状态
    await s.flush()
    for session in doomed.values():
        await messaging.publish_in(s, SessionDeleted(session_id=session.id, agent_id=session.agent_id))
    log.info("sessions deleted", extra={"session_ids": sorted(doomed)})


class AgentSessionsCleanup:
    """删除 agent 流程里会话领域的一步（docs/architecture.md 第 10 节）：订阅 AgentDeleting，在删除 agent 的事务里删除它的
    全部会话；有忙的就 Conflict，整个删除回滚。"""

    def __init__(self, queue: TaskQueue, messaging: Messaging):
        self._queue = queue
        self._messaging = messaging

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentDeleting, self.on_agent_deleting)

    async def on_agent_deleting(self, s: AsyncSession, event: AgentDeleting) -> None:
        sessions = await of_agent_in(s, event.agent_id)
        await delete_sessions(s, self._queue, self._messaging, sessions)
        log.info(
            "sessions of a deleted agent removed",
            extra={"agent": event.agent_id, "session_ids": [x.id for x in sessions]},
        )
