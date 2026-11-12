"""在会话之外进行的工作与会话的接口（docs/architecture.md 第 7 节「占用与结清」）：登记占用、运行中检查、交回结局。

- SessionWork：别的领域（后台任务）登记占用与没开始就放弃（hold_in、abandoned_in，在发送方记下或删掉这项工作的事务里），
  工作结束与运行中检查（ended_in、checked_in，在发送方记下结局或确认工作仍在进行的事务里）。
- 运行中检查的间隔规则（check_interval 定间隔、CheckBeats 记上次检查的时刻）：后台任务与委派共用，节拍在
  platform.checks.WorkWatcher。
- post_check_in：运行中检查投信，与 deliver_in 共用占用的检查。
- deliver_in：交回结局并释放占用，和一封信件同一个事务；占用已被停止清掉、或会话已删除时不投。唯一出口：后台任务
  经 SessionWork.ended_in，委派由 thread 结清时在同一个事务里调用（sessions.delegation）。
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import InvalidInput
from muliao.platform.tasks import TaskQueue
from muliao.sessions.holds import abandon, get_in, hold_in, release
from muliao.sessions.mailbox import has_queued_from, position_in, post_in
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
    """别的领域在会话之外做一项工作时对会话的命令。"""

    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def hold_in(self, s: AsyncSession, session_id: str, holder: str, *, awaited: bool) -> None:
        """登记一项进行中的工作，在发送方记下这项工作的事务里：会话据此知道工作还没做完。awaited：用户是否在等它的结局。"""
        await hold_in(s, session_id, holder, awaited=awaited)

    async def abandoned_in(self, s: AsyncSession, session_id: str, holder: str) -> None:
        """这项工作没开始就放弃了，在发送方删掉它的事务里：清掉占用，不交回结局。占用已被停止清掉、或会话已删除时
        不变。"""
        hold = await hold_of_in(s, session_id, holder)
        if hold is not None and hold.status == HoldStatus.ACTIVE:
            abandon(hold)

    async def ended_in(self, s: AsyncSession, session_id: str, holder: str, text: str) -> None:
        """这项工作结束了，在发送方记下结局的事务里：往会话的信箱投一封事件信件（来源是占用者），同时释放占用。占用
        已被停止清掉、或会话已删除时不投。"""
        await deliver_in(
            s,
            self._queue,
            await hold_of_in(s, session_id, holder),
            source=holder,
            event_text=text,
        )

    async def checked_in(self, s: AsyncSession, session_id: str, holder: str, text: str) -> None:
        """这项工作还在进行，在发送方确认它仍在进行的事务里：给会话投一封检查信件（规则见 post_check_in）。与 ended_in
        互斥，检查信件不会排在结束之后。"""
        await post_check_in(s, self._queue, await hold_of_in(s, session_id, holder), holder, text)


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
    hold: SessionHold | None,
    *,
    source: str,
    event_text: str,
    answer: CallResult | None = None,
) -> Delivered:
    """交回这项占用的结局并释放它，唯一出口。hold 为 None：会话已删除（占用随它删除，见 hold_of_in）。会话还停在
    这项占用对应的挂起调用上、且给了 answer（那一种挂起调用的回答信封）时作为那次调用的结果（回答信件）；否则作为
    事件信件（「迟到的回答改为事件」只在这里）。用户是否在等这封信随占用登记时记下的。交回与结局的记录在同一个事务里，
    所以每项占用只交回一次：已经释放过的再交回是 bug，当场报错。"""
    if hold is None:
        log.info("outcome for a deleted session dropped", extra={"source": source})
        return Delivered.GONE
    session_id = hold.session_id
    before = release(hold)
    if before == HoldStatus.CLEARED:
        log.info(
            "outcome for a stopped session dropped", extra={"session_id": session_id, "hold_id": hold.id}
        )
        return Delivered.DROPPED
    if before == HoldStatus.RELEASED:
        raise RuntimeError(f"hold {hold.id} of session {session_id} was already released by another delivery")
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
        )
        delivered = Delivered.EVENT
    log.info(
        "outcome delivered",
        extra={"session_id": session_id, "hold_id": hold.id, "delivered_as": delivered},
    )
    return delivered


async def post_check_in(
    s: AsyncSession, queue: TaskQueue, hold: SessionHold | None, source: str, text: str
) -> bool:
    """运行中检查（platform.checks.WorkWatcher 的节奏）：在调用方事务里给这项工作的会话投一封检查信件（不推送）；这个
    来源已有信件排着时不投（不堆积）。占用已不是 active（会话被停止）或会话已删除（hold 为 None）时也不投：停止之后
    不唤醒会话。返回是否投了。"""
    if hold is None or hold.status != HoldStatus.ACTIVE:
        return False
    session_id = hold.session_id
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


async def hold_of_in(s: AsyncSession, session_id: str, holder: str) -> SessionHold | None:
    """外部工作在会话上的占用（交回结局与运行中检查用）：会话已删除时 None（占用随会话删除）；会话还在而占用从未登记过
    是 bug。"""
    hold = await get_in(s, session_id, holder)
    if hold is None and await s.get(Session, session_id) is not None:
        raise RuntimeError(f"session {session_id} has no hold {holder}")
    return hold
