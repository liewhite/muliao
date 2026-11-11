"""在会话之外进行的工作与会话的接口（docs/architecture.md 第 7 节「占用与结清」）：登记占用、运行中检查、交回结局。

- SessionWork.hold：别的领域（后台任务）登记占用，在会话领域自己的事务里执行；结束与运行中检查经定向命令
  WorkEnded / WorkCheckIn（同一条按占用者的 lane）。
- 运行中检查的间隔规则（check_interval 定间隔、CheckBeats 记上次检查的时刻）：后台任务与委派共用，节拍在
  platform.checks.WorkWatcher。
- post_check_in：运行中检查投信，与 deliver_in 共用占用的检查。
- deliver_in：交回结局并释放占用，和一封信件同一个事务；占用已被停止清掉、或会话已删除时不投。唯一出口：后台任务
  经定向命令 WorkEnded，委派由 thread 结清时在同一个事务里调用（sessions.delegation）。
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import InvalidInput
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit, TaskQueue
from muliao.sessions.events import WorkCheckIn, WorkEnded
from muliao.sessions.holds import get_in, hold_in, release
from muliao.sessions.lanes import work_lane
from muliao.sessions.mailbox import has_queued_from, position_in, post_in, posted_with_key_in
from muliao.sessions.pending import CallResult, paused
from muliao.sessions.tables import HoldStatus, MailKind, Session, SessionHold

log = logging.getLogger(__name__)

# 发起并发工作时不给 heartbeat_seconds 的默认间隔
DEFAULT_CHECK_SECONDS = 60.0


def check_interval(heartbeat_seconds: float | None) -> float:
    """工具参数 heartbeat_seconds → 运行中检查的间隔（run_shell(background)、delegate_task(report_back) 共用）：不给时取默认，
    不大于 0（含 NaN）时报 InvalidInput。"""
    interval = DEFAULT_CHECK_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    if not interval > 0:
        raise InvalidInput("heartbeat_seconds must be greater than 0")
    return interval


class CheckBeats[K]:
    """运行中检查的节拍：每项工作上一次检查的时刻（还没检查过的以工作开始的时刻算）。只是节拍，重启后从开始的时刻
    重新算，不落库。"""

    def __init__(self) -> None:
        self._last: dict[K, datetime] = {}

    def keep(self, watched: Collection[K]) -> None:
        """只留还在被检查的工作。"""
        for gone in set(self._last) - set(watched):
            del self._last[gone]

    def due(self, key: K, started: datetime, interval_seconds: float, now: datetime) -> bool:
        """上一次检查之后过了一个间隔。"""
        return now - self._last.get(key, started) >= timedelta(seconds=interval_seconds)

    def checked(self, key: K, now: datetime) -> None:
        self._last[key] = now


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
    answer: CallResult | None = None,
) -> Delivered:
    """交回占用 holder 的结局并释放它，唯一出口。会话还停在这项占用对应的挂起调用上、且给了 answer（那一种挂起调用的
    回答信封）时作为那次调用的结果（回答信件）；否则作为事件信件（「迟到的回答改为事件」只在这里）。用户是否在等这封信随占用登记时记下的。
    按 key 幂等。"""
    hold = await _hold_in(s, session_id, holder)
    if hold is None:
        log.info("outcome for a deleted session dropped", extra={"session_id": session_id, "holder": holder})
        return Delivered.GONE
    before = release(hold)
    if before == HoldStatus.CLEARED:
        log.info("outcome for a stopped session dropped", extra={"session_id": session_id, "holder": holder})
        return Delivered.DROPPED
    if before == HoldStatus.RELEASED and not await posted_with_key_in(s, session_id, key):
        # 重放带的是同一个 key（按它去重）；换了 key 再交回一次，是同一项工作被交回了两次
        raise RuntimeError(f"hold {holder} of session {session_id} was already released by another delivery")
    waiting = paused(await position_in(s, session_id))
    if (
        answer is not None
        and hold.call_id is not None
        and waiting is not None
        and hold.call_id in waiting.pending
    ):
        await post_in(
            s,
            queue,
            session_id,
            kind=MailKind.ANSWER,
            content="",
            answers={hold.call_id: answer.model_dump()},
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


async def post_check_in(
    s: AsyncSession, queue: TaskQueue, session_id: str, holder: str, source: str, text: str
) -> bool:
    """运行中检查（platform.checks.WorkWatcher 的节奏）：在调用方事务里给发起会话投一封检查信件（不推送）；这个来源
    已有信件排着时不投（不堆积）。工作的占用已不是 active（会话被停止，或会话已删除）时也不投：停止之后不唤醒会话。
    返回是否投了。"""
    hold = await _hold_in(s, session_id, holder)
    if hold is None or hold.status != HoldStatus.ACTIVE:
        return False
    if await has_queued_from(s, session_id, source):
        return False
    await post_in(
        s,
        queue,
        session_id,
        kind=MailKind.EVENT,
        source=source,
        content=text,
        awaited=False,
        check_in=True,
    )
    return True


async def _hold_in(s: AsyncSession, session_id: str, holder: str) -> SessionHold | None:
    """工作在会话上的占用（交回结局与运行中检查共用）：会话已删除时 None（占用随会话删除）；会话还在而占用从未登记过
    是 bug。"""
    hold = await get_in(s, session_id, holder)
    if hold is None and await s.get(Session, session_id) is not None:
        raise RuntimeError(f"session {session_id} has no hold {holder}")
    return hold


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
