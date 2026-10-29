"""到点唤醒（wakeups 表的唯一读写处，docs/architecture.md 第 7 节「到点唤醒」）：会话用 wake_me 约一个时刻，到点时向它
投一封事件信件（带约的时候写的说明），它接着做。这是会话等待的一种，与后台任务、委派同类：约好之后会话上有一个
active 占用（holder `wake:<会话>:<工具调用 id>`），所以工作状态是 waiting、委派来的工作不结清、不算停滞；到点由
deliver_in 交回并释放，和别的占用走同一个出口。

- 一个会话至多一个还在等的唤醒：再约就取代（旧的占用 abandon），也可以取消；会话被停止时占用被清掉，到点不投。
- 存在库里，重启不丢：到没到点只看 due_at 与占用的状态，节拍由 platform.checks.WorkWatcher 给（WakeUps.check）。
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.db import Database, utcnow
from muliao.platform.tasks import Clock, TaskQueue
from muliao.sessions.holds import abandon, active_holders_in, active_holders_of_in, get_in, hold_in
from muliao.sessions.tables import WakeUp
from muliao.sessions.work import deliver_in

log = logging.getLogger(__name__)

# 工具名，也是唤醒信件的事件来源
WAKE_ME = "wake_me"


def wake_holder(session_id: str, call_id: str) -> str:
    """约唤醒的那次调用在会话上的占用者：按调用 id 确定，续跑重新执行同一次调用时不重复约。"""
    return f"wake:{session_id}:{call_id}"


@dataclass(frozen=True)
class Scheduled:
    due_at: datetime
    # 被这次取代的那个唤醒的时刻；原来没有在等的为 None
    replaced: datetime | None


@dataclass(frozen=True)
class WakeRecord:
    due_at: datetime
    note: str


async def records_among(s: AsyncSession, holders: Collection[str]) -> dict[str, WakeRecord]:
    """这些占用者里是到点唤醒的那些约的时刻与说明（别的占用不在结果里），一次查询。"""
    if not holders:
        return {}
    rows = await s.scalars(select(WakeUp).where(WakeUp.holder.in_(list(holders))))
    return {row.holder: WakeRecord(row.due_at, row.note) for row in rows.all()}


async def cancel_in(s: AsyncSession, session_id: str) -> datetime | None:
    """取消会话还在等的唤醒（占用还 active 的，至多一个），返回它原定的时刻；没有在等的为 None。"""
    active = await active_holders_of_in(s, session_id)
    cancelled = None
    for row in (await s.scalars(select(WakeUp).where(WakeUp.holder.in_(active)))).all():
        hold = await get_in(s, session_id, row.holder)
        assert hold is not None  # 刚按 active 占用者查出
        abandon(hold)
        cancelled = row.due_at
    return cancelled


async def schedule_in(
    s: AsyncSession, session_id: str, call_id: str, due_at: datetime, note: str, *, awaited: bool
) -> Scheduled:
    """约一次唤醒，取代会话还在等的那个。awaited：用户是否在等约它的那次运行的回答（唤醒的信件随之推送）。同一次调用
    重放时返回已约的，不再取代。"""
    holder = wake_holder(session_id, call_id)
    existing = await s.scalar(select(WakeUp).where(WakeUp.holder == holder))
    if existing is not None:
        return Scheduled(existing.due_at, None)
    replaced = await cancel_in(s, session_id)
    await hold_in(s, session_id, holder, awaited=awaited)
    s.add(WakeUp(holder=holder, session_id=session_id, due_at=due_at, note=note, created_at=utcnow()))
    await s.flush()
    return Scheduled(due_at, replaced)


def local_time(at: datetime, tz: ZoneInfo) -> str:
    """给模型看的本地时刻（带时区缩写）。"""
    return at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def wake_text(row: WakeUp, tz: ZoneInfo) -> str:
    return (
        f"Wake-up you scheduled with {WAKE_ME} at {local_time(row.created_at, tz)} is due "
        f"({local_time(row.due_at, tz)}). Your note: {row.note}"
    )


class WakeUps:
    """到点的唤醒（WorkWatcher 的一种工作）：占用还 active、due_at 已到的，各投一封事件信件并释放占用（deliver_in）。"""

    def __init__(self, db: Database, queue: TaskQueue, tz: ZoneInfo, clock: Clock = utcnow):
        self._db = db
        self._queue = queue
        self._tz = tz
        self._clock = clock

    async def check(self) -> int:
        now = self._clock()
        async with self._db.transaction() as s:
            active = await active_holders_in(s)
            due = (
                await s.scalars(
                    select(WakeUp)
                    .where(WakeUp.holder.in_(active), WakeUp.due_at <= now)
                    .order_by(WakeUp.due_at)
                )
            ).all()
            for row in due:
                await deliver_in(
                    s,
                    self._queue,
                    row.session_id,
                    row.holder,
                    key=f"{row.holder}:due",
                    source=WAKE_ME,
                    event_text=wake_text(row, self._tz),
                )
        if due:
            log.info("wake-ups delivered", extra={"holders": [row.holder for row in due]})
        return len(due)
