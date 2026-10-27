"""一次性定时触发：config = {"at": "<ISO 8601 时间>"}，不带时区的按服务端配置的时区解释。

到点触发一次，这一次用掉之后就结束（service.finish_in）：不再出现在触发器列表里，也不能再修改、启用或
立即运行，但行保留着，它发起过的会话仍查得到是谁发的。「用掉」与触发的结果无关——真的投递、前置检查判定没事、
触发失败（原因记在 disabled_reason 上）都算用掉，结束与触发的先后由 triggers.due 保证（至多触发一次）。

用户或 agent 的「立即运行」（TriggerService.run）不消耗它预定的那一次：那是管理操作，用来试跑。

停用期间错过了预定时间的，重新启用时 configure 会拒绝（时间已过去）：要么改成新的时间，要么删掉——
不补一次，也不悄悄顺延。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import InvalidInput
from muliao.platform.db import utcnow
from muliao.platform.tasks import Clock
from muliao.triggers.due import stored_time
from muliao.triggers.service import finish_in, parse_config
from muliao.triggers.tables import Trigger

KIND = "once"


class OnceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime

    @field_validator("at", mode="before")
    @classmethod
    def _iso_text(cls, value: object) -> object:
        # 契约是 ISO 8601 文本，按标准库解析：pydantic 自己的解析还会接受 unix 时间戳（含数字字符串）
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.strip())
            except ValueError:
                pass
        raise ValueError('expected an ISO 8601 time string, e.g. "2026-10-22T10:30"')


class OnceStatus(BaseModel):
    run_at: datetime


class OnceKind:
    name = KIND
    description = (
        'Fires once at a given time and then finishes: config {"at": "<ISO 8601 time>"}, e.g. '
        '{"at": "2026-10-22T10:30"}; without an offset it is read in the server timezone, and it must be in '
        "the future. Use this for anything that should happen once — a cron expression with a date would "
        "fire again every year and stay in the trigger list. Finished triggers leave the list; the "
        "conversations they started stay and can be listed with trigger_list(finished=true)."
    )

    def __init__(self, tz: ZoneInfo, clock: Clock = utcnow):
        self._tz = tz
        self._clock = clock

    def configure(self, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        at = parse_config(KIND, OnceConfig, config).at
        # 不带时区的按服务端时区解释（用户与 agent 说的都是本地时间）
        if at.tzinfo is None:
            at = at.replace(tzinfo=self._tz)
        if at <= self._clock():
            raise InvalidInput(f"once trigger time {config['at']!r} is in the past")
        # 到期时间就是配置本身，没有随运行变化的状态
        return {"at": at.isoformat()}, {}

    def status(self, trigger: Trigger) -> OnceStatus:
        return OnceStatus(run_at=run_at(trigger))


def run_at(trigger: Trigger) -> datetime:
    return stored_time(trigger, trigger.config.get("at"), "time")


class OnceDue:
    """一次性触发的到期规则：到点让它结束，这一次就算用掉了。"""

    kind = KIND

    def due_at(self, trigger: Trigger) -> datetime:
        return run_at(trigger)

    async def consume_in(self, _s: AsyncSession, trigger: Trigger, now: datetime) -> str | None:
        finish_in(trigger, now)
        return None
