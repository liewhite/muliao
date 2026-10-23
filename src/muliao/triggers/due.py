"""按到期时间触发的方式共用的激活机制：扫描到期的触发器，到点各触发一次。

定时（schedule）与一次性（once）的差别只有两件事：到期时间从哪来，以及这次到期被用掉之后这一行怎么变（推进到
下次 / 归档）。两件事由各方式的 DueKind 给出；扫描、并发触发、失败停用与下次唤醒时间在这里实现一次。

用最终正确性换简化：消耗这次到期与触发不在一个事务里（触发可能先跑前置检查，不能占着事务）。进程恰好死在两者
之间时这一次被跳过（至多一次），换来的是检查失败或很慢都不会让同一个时刻反复触发。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import CorruptState, DomainError
from muliao.platform.db import Database, utcnow
from muliao.platform.tasks import Clock
from muliao.triggers.service import TriggerService
from muliao.triggers.tables import Trigger

log = logging.getLogger(__name__)

_MAX_SLEEP_SECONDS = 60.0


class DueKind(Protocol):
    """按到期时间触发的一种方式的激活规则（纯逻辑的那一半是它的 TriggerKind）。"""

    # 这条规则属于哪种 kind
    kind: str

    def due_at(self, trigger: Trigger) -> datetime:
        """这个触发器下次到期的时间。存下的配置或状态不合法时抛 CorruptState（扫描只停用这一个）。"""
        ...

    async def consume_in(self, s: AsyncSession, trigger: Trigger, now: datetime) -> str | None:
        """在到期的事务里消耗这次到期：改这一行，使它不再在 now 到期（定时推进到下次，一次性归档）。

        返回非 None 表示这次不触发（原因记日志）；这次到期无论如何都已经被消耗掉。
        """
        ...


def stored_time(trigger: Trigger, value: object, what: str) -> datetime:
    """读出触发器存下的到期时间（ISO 8601、带时区，配置时总是这样写入）。缺失、解析不了或不带时区都是存下的数据坏了：
    抛 CorruptState，信息里带上是哪个触发器。"""
    try:
        at = datetime.fromisoformat(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise CorruptState(f"trigger {trigger.id} ({trigger.name}) has no valid {what}: {value!r}") from e
    if at.tzinfo is None:
        raise CorruptState(f"trigger {trigger.id} ({trigger.name}) has a {what} without timezone: {value!r}")
    return at


class DueRunner:
    """到期扫描循环：列出到期的触发器，并发地各触发一次。"""

    def __init__(
        self, db: Database, triggers: TriggerService, kinds: Sequence[DueKind], clock: Clock = utcnow
    ):
        self._db = db
        self._triggers = triggers
        self._kinds = {k.kind: k for k in kinds}
        self._clock = clock
        self._changed = triggers.watch()

    async def _pending(self) -> list[tuple[Trigger, datetime]]:
        """还会到期的触发器与它们的到期时间（已归档与停用的不在列表里）。

        存下的数据坏了、算不出到期时间的停用（记 disabled_reason），不让一行坏数据拖垮整个扫描循环。
        """
        pending = []
        for t in await self._triggers.list(enabled_only=True):
            if t.kind not in self._kinds:
                continue
            try:
                pending.append((t, self._kinds[t.kind].due_at(t)))
            except CorruptState as e:
                await self._disable(t.id, f"{type(e).__name__}: {e}")
        return pending

    async def fire_due(self) -> int:
        now = self._clock()
        due = [t.id for t, at in await self._pending() if at <= now]
        # 到期的触发器并发执行：一个慢的前置检查不拖住同一轮里的其他触发器。意外的异常（契约之外）让整组取消并上抛，
        # 由后台循环的崩溃处理暴露，不会留下没人接收的异常
        async with anyio.create_task_group() as group:
            for trigger_id in due:
                group.start_soon(self._fire_one, trigger_id, now)
        return len(due)

    async def _fire_one(self, trigger_id: int, now: datetime) -> None:
        try:
            if not await self._consume(trigger_id, now):
                return
        except (CorruptState, DomainError) as e:
            # 只有这个触发器自己存下的数据（到期规则读的配置与状态）坏了才停用它；触发链路上别处的 CorruptState 照常上抛
            await self._disable(trigger_id, f"{type(e).__name__}: {e}")
            return
        # 触发本身的失败按激活机制共用的规则处理（见 TriggerService.fire_activated）
        await self._triggers.fire_activated(trigger_id)

    async def _consume(self, trigger_id: int, now: datetime) -> bool:
        """按最新的行消耗这次到期；返回是否应当触发。"""
        async with self._db.transaction() as s:
            trigger = await s.get(Trigger, trigger_id)
            # 列出之后可能已被删除、停用、归档或改了配置：按最新的行重新判断
            skip = self._skip_reason(trigger, now)
            if trigger is None or skip is not None:
                log.info(
                    "due fire skipped: trigger %s since it was due", skip, extra={"trigger_id": trigger_id}
                )
                return False
            skipped = await self._kinds[trigger.kind].consume_in(s, trigger, now)
            if skipped is not None:
                log.info("due fire skipped: %s", skipped, extra={"trigger": trigger.name})
                return False
        return True

    def _skip_reason(self, trigger: Trigger | None, now: datetime) -> str | None:
        """列出到期之后、触发之前触发器发生的变化；None 表示仍应触发。"""
        if trigger is None:
            return "deleted"
        if trigger.finished_at is not None:
            return "finished"
        if not trigger.enabled:
            return "disabled"
        if self._kinds[trigger.kind].due_at(trigger) > now:
            return "rescheduled"
        return None

    async def _disable(self, trigger_id: int, error: str) -> None:
        if not await self._triggers.disable(trigger_id, error):
            log.info("failed trigger was deleted meanwhile", extra={"trigger_id": trigger_id, "error": error})
            return
        log.error("trigger disabled: %s", error, extra={"trigger_id": trigger_id})

    async def _next_wake(self) -> float:
        times = [at for _, at in await self._pending()]
        if not times:
            return _MAX_SLEEP_SECONDS
        return min(_MAX_SLEEP_SECONDS, max(0.0, (min(times) - self._clock()).total_seconds()))

    async def run_forever(self) -> None:
        while True:
            await self.fire_due()
            self._changed.clear()
            with anyio.move_on_after(await self._next_wake()):
                await self._changed.wait()
