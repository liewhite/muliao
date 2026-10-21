"""一次性定时触发：config = {"at": "<ISO 8601 时间>"}，不带时区的按服务端配置的时区解释。

到点触发一次，这一次用掉之后就归档（TriggerService.finish_in）：不再出现在触发器列表里，也不能再修改、启用或
立即运行，但行保留着，它发起过的会话仍查得到是谁发的。「用掉」与触发的结果无关——真的投递、前置检查判定没事、
触发失败（原因记在 disabled_reason 上）都算用掉，归档与触发的先后由 triggers.due 保证（至多触发一次）。

用户或 agent 的「立即运行」（TriggerService.run）不消耗它预定的那一次：那是管理操作，用来试跑。

停用期间错过了预定时间的，重新启用时 configure 会拒绝（时间已过去）：要么改成新的时间，要么删掉——
不补一次，也不悄悄顺延。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import InvalidInput
from muliao.platform.db import utcnow
from muliao.platform.tasks import Clock
from muliao.triggers.service import TriggerService
from muliao.triggers.tables import Trigger

KIND = "once"


class OnceConfig(BaseModel):
    at: datetime


class OnceStatus(BaseModel):
    run_at: datetime


class OnceKind:
    name = KIND
    description = (
        'Fires once at a given time and is then archived: config {"at": "<ISO 8601 time>"}, e.g. '
        '{"at": "2026-10-22T10:30"}; without an offset it is read in the server timezone, and it must be in '
        "the future. Use this for anything that should happen once — a cron expression with a date would "
        "fire again every year and stay in the trigger list. Archived triggers leave the list; the "
        "conversations they started stay and can be listed with trigger_list(finished=true)."
    )

    def __init__(self, tz: ZoneInfo, clock: Clock = utcnow):
        self._tz = tz
        self._clock = clock

    def configure(self, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if set(config) != {"at"} or not isinstance(config["at"], str):
            raise InvalidInput(f'once trigger config must be {{"at": "<ISO 8601 time>"}}, got {config!r}')
        at = self._parse(config["at"])
        if at <= self._clock():
            raise InvalidInput(f"once trigger time {config['at']!r} is in the past")
        # 到期时间就是配置本身，没有随运行变化的状态
        return {"at": at.isoformat()}, {}

    def _parse(self, text: str) -> datetime:
        try:
            at = datetime.fromisoformat(text.strip())
        except ValueError as e:
            raise InvalidInput(
                f"invalid once trigger time {text!r}: expected ISO 8601, e.g. 2026-10-22T10:30"
            ) from e
        # 不带时区的按服务端时区解释（用户与 agent 说的都是本地时间）
        return at if at.tzinfo is not None else at.replace(tzinfo=self._tz)

    def status(self, trigger: Trigger) -> OnceStatus:
        return OnceStatus(run_at=run_at(trigger.config))


def run_at(config: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(config["at"])


class OnceDue:
    """一次性触发的到期规则：到点把它归档，这一次就算用掉了。"""

    kind = KIND

    def __init__(self, triggers: TriggerService):
        self._triggers = triggers

    def due_at(self, trigger: Trigger) -> datetime:
        return run_at(trigger.config)

    async def consume_in(self, _s: AsyncSession, trigger: Trigger, _now: datetime) -> str | None:
        self._triggers.finish_in(trigger)
        return None
