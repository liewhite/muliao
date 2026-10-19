"""定时触发：config = {"cron": "<5 段 cron 表达式>"}，按服务端配置的时区解释。

策略：
- 错过的触发（进程停机期间）只补一次，然后从当前时间算下一次。
- 不重叠：上一次触发的会话仍有未完成的一轮时跳过本次，并记 info 日志。
- 触发器自身的数据不合法时停用它并记录原因，不阻塞其他触发器；修复后由用户重新启用。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from muliao.db import Database, utcnow
from muliao.errors import DomainError, InvalidInput
from muliao.models import Trigger
from muliao.runtime.tasks import Clock
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
        if not croniter.is_valid(expr):
            raise InvalidInput(f"invalid cron expression {expr!r}")
        return croniter(expr, after.astimezone(self._tz)).get_next(datetime)

    def configure(
        self, config: dict[str, Any], state: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if set(config) != {"cron"} or not isinstance(config["cron"], str):
            raise InvalidInput(f'schedule trigger config must be {{"cron": "<expression>"}}, got {config!r}')
        expr = config["cron"].strip()
        return {"cron": expr}, {**state, "next_run_at": self.next_run(expr, self._clock()).isoformat()}

    def status(self, trigger: Trigger) -> dict[str, Any]:
        return {"next_run_at": trigger.state["next_run_at"]}


def next_run_at(state: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(state["next_run_at"])


class ScheduleRunner:
    """定时触发的激活机制：到点调用 TriggerService.fire_in。"""

    def __init__(self, db: Database, triggers: TriggerService, kind: ScheduleKind, clock: Clock = utcnow):
        self._db = db
        self._triggers = triggers
        self._kind = kind
        self._clock = clock

    async def fire_due(self) -> int:
        now = self._clock()
        due = [
            t.id
            for t in await self._triggers.list(kind=KIND, enabled_only=True)
            if next_run_at(t.state) <= now
        ]
        for trigger_id in due:
            try:
                await self._fire(trigger_id, now)
            except DomainError as e:
                await self._disable(trigger_id, f"{type(e).__name__}: {e}")
        return len(due)

    async def _fire(self, trigger_id: int, now: datetime) -> None:
        async with self._db.transaction() as s:
            trigger = await self._triggers.get_in(s, trigger_id)
            self._triggers.set_state(
                trigger, next_run_at=self._kind.next_run(trigger.config["cron"], now).isoformat()
            )
            if await self._triggers.busy_in(s, trigger):
                log.info(
                    "scheduled fire skipped: previous run still in progress", extra={"trigger": trigger.name}
                )
                return
            await self._triggers.fire_in(s, trigger, None, None)

    async def _disable(self, trigger_id: int, error: str) -> None:
        async with self._db.transaction() as s:
            trigger = await self._triggers.get_in(s, trigger_id)
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
            self._triggers.changed.clear()
            try:
                await asyncio.wait_for(self._triggers.changed.wait(), timeout=await self._next_wake())
            except TimeoutError:
                pass
