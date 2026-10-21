"""定时触发：config = {"cron": "<5 段 cron 表达式>"}，按服务端配置的时区解释。

到期扫描循环是所有按时间触发的方式共用的（triggers.due.DueRunner），这里只给出定时这一种的规则：

- 下次时间记在 state["next_run_at"]；到点先推进到下次（消耗这次到期），再触发。
- 错过的触发（进程停机期间）只补一次，然后从当前时间算下一次。
- 不重叠：上一次触发的会话仍有未完成的运行时跳过本次，并记 info 日志。
- 停用后重新启用，下次时间从启用时算起（TriggerService 在重新启用时重新推导 state），不补暂停期间的触发。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import InvalidInput
from muliao.platform.db import utcnow
from muliao.platform.tasks import Clock
from muliao.triggers.service import TriggerService
from muliao.triggers.tables import Trigger

KIND = "schedule"


class ScheduleKind:
    name = KIND
    description = (
        'Fires on a schedule. config: {"cron": "<5-field cron expression>"} in the server timezone, '
        'e.g. {"cron": "*/5 * * * *"}. Skips a firing while the previous run is unfinished. For a task that '
        "should happen once, use the `once` kind: a cron expression with a date would fire again every year."
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


class ScheduleDue:
    """定时触发的到期规则：到点推进到下次时间，上一次触发的会话还没做完时跳过这一次。"""

    kind = KIND

    def __init__(self, triggers: TriggerService, kind: ScheduleKind):
        self._triggers = triggers
        self._kind = kind

    def due_at(self, trigger: Trigger) -> datetime:
        return next_run_at(trigger.state)

    async def consume_in(self, s: AsyncSession, trigger: Trigger, now: datetime) -> str | None:
        self._triggers.set_state(
            trigger, next_run_at=self._kind.next_run(trigger.config["cron"], now).isoformat()
        )
        if await self._triggers.busy_in(s, trigger):
            return "previous run still in progress"
        return None
