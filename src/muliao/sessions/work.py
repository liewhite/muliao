"""在会话之外进行的工作与会话的接口（docs/architecture.md 第 7 节「占用与结清」）：登记占用、运行中检查、交回结局。

- SessionWork.hold：别的领域（后台任务）登记占用，在会话领域自己的事务里执行；结束与运行中检查经定向命令
  WorkEnded / WorkCheckIn（同一条按占用者的 lane）。
- deliver_in：交回结局并释放占用，和一封信件同一个事务；占用已被停止清掉、或会话已删除时不投。唯一出口：后台任务
  经定向命令 WorkEnded，委派由 thread 结清时在同一个事务里调用（sessions.delegation）。
"""

from __future__ import annotations

import logging
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit, TaskQueue
from muliao.sessions.events import WorkCheckIn, WorkEnded
from muliao.sessions.holds import get_in, hold_in, release
from muliao.sessions.lanes import work_lane
from muliao.sessions.mailbox import post_check_in, post_in
from muliao.sessions.pending import AgentAnswer, AgentAnswered, pending_in
from muliao.sessions.tables import HoldStatus, Mail, MailKind, Session

log = logging.getLogger(__name__)


class SessionWork:
    """别的领域在会话之外做一项工作时对会话的命令，每个在会话领域自己的事务里执行。"""

    def __init__(self, db: Database):
        self._db = db

    async def hold(self, session_id: str, holder: str, *, awaited: bool) -> None:
        """登记一项进行中的工作（重放幂等）：会话据此知道工作还没做完。awaited：用户是否在等它的结局。"""
        async with self._db.transaction() as s:
            await hold_in(s, session_id, holder, awaited=awaited)


class Delivered(StrEnum):
    """交回结局的结果（命令的结果类型，契约内的情况不靠异常表达）。"""

    # 作为挂起调用的结果（回答信件）
    ANSWER = "answer"
    # 作为事件信件
    EVENT = "event"
    # 占用已被停止清掉：不投
    DROPPED = "dropped"
    # 会话已删除
    GONE = "gone"


async def deliver_in(
    s: AsyncSession,
    queue: TaskQueue,
    session_id: str,
    holder: str,
    *,
    key: str,
    source: str,
    event_text: str,
    answer: AgentAnswer | None = None,
) -> Delivered:
    """交回占用 holder 的结局并释放它，唯一出口。会话还停在这项占用对应的挂起调用上、且给了 answer 时作为那次调用
    的结果（回答信件）；否则作为事件信件（「迟到的回答改为事件」只在这里）。用户是否在等这封信随占用登记时记下的。
    按 key 幂等。"""
    if await s.get(Session, session_id) is None:
        log.info("outcome for a deleted session dropped", extra={"session_id": session_id, "holder": holder})
        return Delivered.GONE
    hold = await get_in(s, session_id, holder)
    if hold is None:
        # 会话存在而占用从未登记过是 bug
        raise RuntimeError(f"session {session_id} has no hold {holder}")
    before = release(hold)
    if before == HoldStatus.CLEARED:
        log.info("outcome for a stopped session dropped", extra={"session_id": session_id, "holder": holder})
        return Delivered.DROPPED
    if (
        before == HoldStatus.RELEASED
        and (await s.scalar(select(Mail.id).where(Mail.session_id == session_id, Mail.dedupe_key == key)))
        is None
    ):
        # 重放带的是同一个 key（按它去重）；换了 key 再交回一次，是同一项工作被交回了两次
        raise RuntimeError(f"hold {holder} of session {session_id} was already released by another delivery")
    if answer is not None and hold.call_id is not None and hold.call_id in await pending_in(s, session_id):
        await post_in(
            s,
            queue,
            session_id,
            kind=MailKind.ANSWER,
            content="",
            answers={hold.call_id: AgentAnswered.of(answer).model_dump()},
            awaited=hold.awaited,
            dedupe_key=key,
        )
        delivered = Delivered.ANSWER
    else:
        await post_in(
            s,
            queue,
            session_id,
            kind=MailKind.EVENT,
            source=source,
            content=event_text,
            awaited=hold.awaited,
            dedupe_key=key,
        )
        delivered = Delivered.EVENT
    log.info(
        "outcome delivered",
        extra={"session_id": session_id, "holder": holder, "delivered_as": delivered, "key": key},
    )
    return delivered


def handle_work_commands(messaging: Messaging, queue: TaskQueue) -> None:
    """登记 WorkEnded 与 WorkCheckIn 的处理者（会话领域）：投信、释放占用与这条投递的完成在同一个事务里。"""

    async def ended(command: WorkEnded, _delivery: Delivery) -> Commit:
        async def commit(s: AsyncSession) -> None:
            await deliver_in(
                s,
                queue,
                command.session_id,
                command.holder,
                key=f"{command.holder}:end",
                source=command.holder,
                event_text=command.text,
            )

        return commit

    async def check_in(command: WorkCheckIn, _delivery: Delivery) -> Commit:
        async def commit(s: AsyncSession) -> None:
            await post_check_in(s, queue, command.session_id, command.holder, command.holder, command.text)

        return commit

    messaging.handle(WorkEnded, "sessions.work_ended", ended, lane=lambda c: work_lane(c.holder))
    messaging.handle(WorkCheckIn, "sessions.work_check_in", check_in, lane=lambda c: work_lane(c.holder))
