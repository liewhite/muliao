"""删除会话的唯一规则与删除 agent 流程里会话领域的一步（docs/architecture.md 第 7 节「thread 与结果路由」）。"""

import logging

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict, TransientError
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit, TaskQueue
from muliao.sessions.activity import blocking_deletion_in, sessions_blocking_agent_deletion_in
from muliao.sessions.directory import of_agent_in, owned_threads_in
from muliao.sessions.events import AgentSessionsCleared, SessionDeleted
from muliao.sessions.halt import StopSession
from muliao.sessions.lanes import agent_deleting_lane
from muliao.sessions.settlement import settle_deleted_in
from muliao.sessions.tables import Session

log = logging.getLogger(__name__)


async def delete_sessions(
    s: AsyncSession, queue: TaskQueue, messaging: Messaging, roots: list[Session]
) -> None:
    """删除会话的唯一规则（删会话、删 agent 都经过这里）。

    私有 thread 只对父会话有意义，逐层随父会话删除（用户不能单独删除它，见 SessionService.delete）；用户可见的
    thread 保留为独立会话（parent 置空）。
    任一被删会话忙时拒绝（判断只看会话自己的数据），避免运行结果无处提交、工具写到被删的目录。交给被删会话、
    还没结清的委派结清为「被删除」，结局交回发起方（sessions.delegation）。
    """
    doomed = await owned_threads_in(s, roots)
    for session_id in await blocking_deletion_in(s, queue, doomed):
        # 错误也会交还给模型，不带 id；定位信息记在日志里
        log.info("session deletion refused: busy", extra={"session_id": session_id})
        what = "the session" if session_id in {x.id for x in roots} else "one of the threads it owns"
        raise Conflict(
            f"{what} is busy (a run is queued or running, or background jobs or delegated work are still "
            "going); stop it first, then delete"
        )
    for session in doomed.values():
        # 交给被删会话、还没结清的工作交回「被删除」
        await settle_deleted_in(s, queue, session)
    for session in doomed.values():
        await s.delete(session)
        messaging.publish_in(s, SessionDeleted(session_id=session.id, agent_id=session.agent_id))
    # 立即执行删除：同一事务里随后的查询（如删除 agent 的流程）看到的是删除之后的状态
    await s.flush()
    log.info("sessions deleted", extra={"session_ids": sorted(doomed)})


# 删除 agent 时停止它忙的会话之后，等它们空闲的时限与查看间隔
_IDLE_WAIT_SECONDS = 30.0
_IDLE_POLL_SECONDS = 0.1


class AgentSessionsCleanup:
    """删除 agent 流程里会话领域的一步（docs/architecture.md 第 10 节）：订阅 AgentDeleting，删除它的全部会话，发布
    AgentSessionsCleared。stop_session 是停止会话的命令（sessions.halt.StopSession）。"""

    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        messaging: Messaging,
        stop_session: StopSession,
    ):
        self._db = db
        self._queue = queue
        self._messaging = messaging
        self._stop_session = stop_session

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(
            AgentDeleting,
            "sessions.agent_deleting",
            self.on_agent_deleting,
            lane=lambda e: agent_deleting_lane(e.agent_id),
        )

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：删除它的全部会话（连同私有 thread），发布 AgentSessionsCleared（没有会话也发）。

        标记删除之后它的会话不再收到来信（inbound 在事务里用 active_agent_in 检查），但仍可能有会话忙：删除前的检查与标记分属两个
        事务，之间来的信件排上的运行会在标记之后执行、可能开出后台任务或委派；人为重跑它的任务也会让会话变忙。
        忙的会话先停止（每个只停一次；停止也本来是删除的前提：它们的工作随 agent 一起放弃），再在时限内等它们空闲：
        正在执行的运行被取消后，停止事务在它退出之后才执行（清掉占用、结束排队的任务），正在执行的压缩做完才空闲。
        时限内没有空闲（压缩卡住、或停止之后又被人为重跑）就按暂时失败重试，重试时同样先停止再等。"""
        await self._stop_and_wait_idle(event.agent_id)

        async def commit(s: AsyncSession) -> None:
            sessions = await of_agent_in(s, event.agent_id)
            try:
                await delete_sessions(s, self._queue, self._messaging, sessions)
            except Conflict as e:
                log.warning(
                    "deleted agent still has a busy session, will retry", extra={"agent": event.agent_id}
                )
                raise TransientError(f"agent {event.agent_id} still has a busy session: {e}") from e
            self._messaging.publish_in(s, AgentSessionsCleared(agent_id=event.agent_id))
            log.info(
                "sessions of a deleted agent removed",
                extra={"agent": event.agent_id, "session_ids": [x.id for x in sessions]},
            )

        return commit

    async def _stop_and_wait_idle(self, agent_id: str) -> None:
        stopped: set[str] = set()
        with anyio.move_on_after(_IDLE_WAIT_SECONDS):
            while True:
                async with self._db.transaction() as s:
                    busy = await sessions_blocking_agent_deletion_in(s, self._queue, agent_id)
                if not busy:
                    return
                for session_id in sorted(set(busy) - stopped):
                    halted = await self._stop_session(session_id)
                    stopped.add(session_id)
                    log.info(
                        "busy session of a deleted agent stopped",
                        extra={"agent": agent_id, "session_id": session_id, "halted": halted},
                    )
                await anyio.sleep(_IDLE_POLL_SECONDS)
        log.warning(
            "sessions of a deleted agent still busy after stopping them",
            extra={"agent": agent_id, "waited_seconds": _IDLE_WAIT_SECONDS},
        )
