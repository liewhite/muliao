"""定时触发：config = {"cron": "<5 段 cron 表达式>"}，按服务端配置的时区解释。

策略：
- 错过的触发（进程停机期间）只补一次，然后从当前时间算下一次。
- 不重叠：上一次触发的会话仍有未完成的运行时跳过本次，并记 info 日志。
- 触发时被拒绝（例如处理说明为空、cron 表达式不合法）时停用它并记录原因，不阻塞其他触发器；修复后由用户重新启用。
- 列出到期触发器之后、触发之前，触发器可能被删除、停用或改了配置：触发前按最新的行重新判断，不再到期就跳过。
- 停用后重新启用，下次时间从启用时算起（TriggerService 在重新启用时重新推导 state），不补暂停期间的触发。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from muliao.db import Database, utcnow
from muliao.errors import Conflict, DomainError, InvalidInput, NotFound
from muliao.models import Trigger
from muliao.runtime.tasks import Clock
from muliao.triggers.precheck import PrecheckFailed
from muliao.triggers.service import TriggerService

log = logging.getLogger(__name__)

KIND = "schedule"
_MAX_SLEEP_SECONDS = 60.0


class ScheduleKind:
    name = KIND
    description = (
        'Fires on a schedule. config: {"cron": "<5-field cron expression>"} in the server timezone, '
        'e.g. {"cron": "*/5 * * * *"}. Skips a firing while the previous run is unfinished.'
    )

    def __init__(self, tz: ZoneInfo, clock: Clock = utcnow):
        self._tz = tz
        self._clock = clock

    def next_run(self, expr: str, after: datetime) -> datetime:
        # 契约是 5 段表达式（分 时 日 月 周）。croniter 也接受带秒的 6 段，`* * * * * *` 会每秒到期
        if len(expr.split()) != 5 or not croniter.is_valid(expr):
            raise InvalidInput(
                f"invalid cron expression {expr!r}: expected 5 fields (minute hour day month weekday)"
            )
        return croniter(expr, after.astimezone(self._tz)).get_next(datetime)

    def configure(self, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if set(config) != {"cron"} or not isinstance(config["cron"], str):
            raise InvalidInput(f'schedule trigger config must be {{"cron": "<expression>"}}, got {config!r}')
        expr = config["cron"].strip()
        return {"cron": expr}, {"next_run_at": self.next_run(expr, self._clock()).isoformat()}

    def status(self, trigger: Trigger) -> dict[str, Any]:
        return {"next_run_at": trigger.state["next_run_at"]}


def next_run_at(state: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(state["next_run_at"])


def _skip_reason(trigger: Trigger | None, now: datetime) -> str | None:
    """列出到期之后、触发之前触发器发生的变化；None 表示仍应触发。"""
    if trigger is None:
        return "deleted"
    if not trigger.enabled:
        return "disabled"
    if next_run_at(trigger.state) > now:
        return "rescheduled"
    return None


class ScheduleRunner:
    """定时触发的激活机制：到点推进下次时间，再调用 TriggerService.fire。

    用最终正确性换简化：推进下次时间与触发不在一个事务里（触发可能先跑前置检查，不能占着事务）。进程恰好死在
    两者之间时这一次被跳过（至多一次），换来的是检查失败或很慢都不会让同一个时刻反复触发。
    """

    def __init__(self, db: Database, triggers: TriggerService, kind: ScheduleKind, clock: Clock = utcnow):
        self._db = db
        self._triggers = triggers
        self._kind = kind
        self._clock = clock
        self._changed = triggers.subscribe()

    async def fire_due(self) -> int:
        now = self._clock()
        due = [
            t.id
            for t in await self._triggers.list(kind=KIND, enabled_only=True)
            if next_run_at(t.state) <= now
        ]
        # 到期的触发器并发执行：一个慢的前置检查不拖住同一轮里的其他触发器。意外的异常（契约之外）让整组取消并上抛，
        # 由后台循环的崩溃处理暴露，不会留下没人接收的异常
        async with asyncio.TaskGroup() as group:
            for trigger_id in due:
                group.create_task(self._fire_one(trigger_id, now))
        return len(due)

    async def _fire_one(self, trigger_id: int, now: datetime) -> None:
        try:
            await self._fire(trigger_id, now)
        except PrecheckFailed:
            # 已记在触发器上；脚本的故障往往是暂时的，保持启用，下次照常检查
            pass
        except (Conflict, NotFound) as e:
            # 推进时间之后、触发之前被用户停用或删除：不是触发失败，不覆盖失败原因
            log.info("scheduled fire skipped: %s", e, extra={"trigger_id": trigger_id})
        except DomainError as e:
            await self._disable(trigger_id, f"{type(e).__name__}: {e}")

    async def _fire(self, trigger_id: int, now: datetime) -> None:
        async with self._db.transaction() as s:
            trigger = await s.get(Trigger, trigger_id)
            # 列出之后可能已被删除、停用或改了配置：按最新的行重新判断
            skip = _skip_reason(trigger, now)
            if skip is not None:
                log.info(
                    "scheduled fire skipped: trigger %s since it was due",
                    skip,
                    extra={"trigger_id": trigger_id},
                )
                return
            self._triggers.set_state(
                trigger, next_run_at=self._kind.next_run(trigger.config["cron"], now).isoformat()
            )
            if await self._triggers.busy_in(s, trigger):
                log.info(
                    "scheduled fire skipped: previous run still in progress", extra={"trigger": trigger.name}
                )
                return
        await self._triggers.fire(trigger_id)

    async def _disable(self, trigger_id: int, error: str) -> None:
        async with self._db.transaction() as s:
            trigger = await s.get(Trigger, trigger_id)
            if trigger is None:
                log.info(
                    "failed trigger was deleted meanwhile", extra={"trigger_id": trigger_id, "error": error}
                )
                return
            trigger.enabled = False
            trigger.last_error = error
        log.error("schedule trigger disabled after failed fire: %s", error, extra={"trigger_id": trigger_id})

    async def _next_wake(self) -> float:
        times = [next_run_at(t.state) for t in await self._triggers.list(kind=KIND, enabled_only=True)]
        if not times:
            return _MAX_SLEEP_SECONDS
        return min(_MAX_SLEEP_SECONDS, max(0.0, (min(times) - self._clock()).total_seconds()))

    async def run_forever(self) -> None:
        while True:
            await self.fire_due()
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=await self._next_wake())
            except TimeoutError:
                pass
