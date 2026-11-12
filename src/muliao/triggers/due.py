"""按到期时间触发的方式共用的激活机制：扫描到期的触发器，到点各触发一次。

定时（schedule）与一次性（once）的差别只有两件事：到期时间从哪来，以及这次到期被用掉之后这一行怎么变（推进到
下次 / 结束）。两件事由各方式的 DueKind 给出；扫描、并发触发、失败停用与下次唤醒时间在这里实现一次。

用最终正确性换简化：消耗这次到期与触发不在一个事务里（触发可能先跑前置检查，不能占着事务）。进程恰好死在两者
之间时这一次被跳过（至多一次），换来的是检查失败或很慢都不会让同一个时刻反复触发。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

import anyio
from anyio.abc import TaskGroup
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import CorruptState
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
        """在到期的事务里消耗这次到期：改这一行，使它不再在 now 到期（定时推进到下次，一次性的结束）。

        返回非 None 表示这次不触发（原因记日志）；这次到期无论如何都已经被消耗掉。存下的配置或状态不合法时抛 CorruptState（停用这一个）。
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
        # 触发还没做完的触发器（前置检查在跑时还没有会话，定时的「上一次会话不空闲就跳过」看不到它）：它再到期时只消耗不触发，
        # 同一个触发器不重叠。只在内存：服务重启时在跑的触发随进程结束
        self._firing: set[int] = set()

    async def _pending(self) -> list[tuple[Trigger, datetime]]:
        """还会到期的触发器与它们的到期时间（已结束与停用的不在列表里）。

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
        """扫描一轮并等这轮的触发都做完；返回到期的个数。"""
        async with anyio.create_task_group() as group:
            due = await self._fire_due_in(group)
        return due  # pyright: ignore[reportPossiblyUnboundVariable]  # 任务组 __aexit__ 标注为 bool，被当成可能吞掉异常

    async def _fire_due_in(self, group: TaskGroup) -> int:
        """扫描一轮：到期的在这里逐个消耗（短事务，下一轮不会再看到它到期），触发放进 group 并发跑、不等它做完：
        一个慢的前置检查既不拖住同一轮的其他触发器，也不推迟下一轮扫描。"""
        now = self._clock()
        due = [t.id for t, at in await self._pending() if at <= now]
        for trigger_id in due:
            if not await self._consume_one(trigger_id, now):
                continue
            if trigger_id in self._firing:
                log.info(
                    "due fire skipped: its previous fire is still running", extra={"trigger_id": trigger_id}
                )
                continue
            self._firing.add(trigger_id)
            group.start_soon(self._fire, trigger_id)
        return len(due)

    async def _consume_one(self, trigger_id: int, now: datetime) -> bool:
        """消耗一个到期的触发器，返回是否应当触发。契约之外的异常（如数据库暂时不可用、bug）只记日志、不拖崩扫描循环
        （与流式触发的投递循环同一做法，triggers.stream）：消耗没提交的下一轮重来。"""
        try:
            try:
                return await self._consume(trigger_id, now)
            except CorruptState as e:
                # 只有这个触发器自己存下的数据（到期规则读的配置与状态）坏了才停用它
                await self._disable(trigger_id, f"{type(e).__name__}: {e}")
                return False
        except Exception:
            log.exception("due consume failed", extra={"trigger_id": trigger_id})
            return False

    async def _fire(self, trigger_id: int) -> None:
        """触发一个已消耗的触发器。失败按激活机制共用的规则处理（TriggerService.fire_activated）；契约之外的异常只记在
        它自己身上，不取消同一组里的其他触发，也不再重试（至多一次）。"""
        try:
            await self._triggers.fire_activated(trigger_id)
        except Exception:
            log.exception("due fire failed", extra={"trigger_id": trigger_id})
        finally:
            self._firing.discard(trigger_id)

    async def _consume(self, trigger_id: int, now: datetime) -> bool:
        """按最新的行消耗这次到期；返回是否应当触发。"""
        async with self._db.transaction() as s:
            trigger = await s.get(Trigger, trigger_id)
            # 列出之后可能已被删除、停用、结束或改了配置：按最新的行重新判断
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
        async with anyio.create_task_group() as group:
            while True:
                await self._fire_due_in(group)
                self._changed.clear()
                with anyio.move_on_after(await self._next_wake()):
                    await self._changed.wait()
