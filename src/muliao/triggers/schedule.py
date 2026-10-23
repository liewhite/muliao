"""定时触发：config = {"cron": "<5 段 cron 表达式>"}，按服务端配置的时区解释。

到期扫描循环是所有按时间触发的方式共用的（triggers.due.DueRunner），这里只给出定时这一种的规则：

- 下次时间记在 state["next_run_at"]；到点先推进到下次（消耗这次到期），再触发。
- 错过的触发（进程停机期间）只补一次，然后从当前时间算下一次。
- 不重叠：上一次触发的会话还没做完（定义见 previous_fire_state_in）时跳过本次，并记 info 日志。
- 跳过要看得见：连续跳过记在 state["skipped"]（次数、从何时起、挡路的会话在运行还是在等待），作为状态给界面与
  模型看；挡路的会话在等待（后台任务、委派、用户回答——可能永远等不到，如没人回答的提问）时通知用户，每轮连续
  跳过只通知一次。在运行的不通知：运行偶尔长过一个周期是常态。下次真正触发、改配置或重新启用时清除；挡路的会话换了（手动「立即
  运行」开了新会话）算新的一轮。
- 停用后重新启用，下次时间从启用时算起（TriggerService 在重新启用时重新推导 state），不补暂停期间的触发。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import CroniterBadDateError, croniter
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import CorruptState, InvalidInput
from muliao.notifications.events import Notify
from muliao.platform.db import utcnow
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import Clock
from muliao.sessions.api import SessionActivity, WorkState
from muliao.triggers.due import stored_time
from muliao.triggers.service import parse_config, previous_fire_state_in, set_state
from muliao.triggers.tables import Trigger

KIND = "schedule"


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cron: str


class SkippedFires(BaseModel):
    # 连续跳过了几次、第一次跳过的时间，以及最近一次跳过时上一次触发的会话（last_session_id）的工作状态（不会是 idle）
    count: int
    since: datetime
    previous_state: WorkState


class _SkipStreak(SkippedFires):
    # 存在 state["skipped"] 里的样子，多记两件不给界面看的事：挡路的是哪个会话（手动「立即运行」换了会话就是新的一轮），
    # 以及这一轮是否已通知过用户
    session_id: str
    notified: bool


class ScheduleStatus(BaseModel):
    next_run_at: datetime
    # 正在连续跳过（上一次触发的会话还没做完）时给出，下次真正触发时清除
    skipped: SkippedFires | None


class ScheduleKind:
    name = KIND
    description = (
        'Fires on a schedule. config: {"cron": "<5-field cron expression>"} in the server timezone, '
        'e.g. {"cron": "*/5 * * * *"}. Skips a firing while the conversation the previous firing started is '
        "still busy: running, or waiting on background jobs, delegated work or the user's answer; status.skipped "
        "then counts the consecutive skips, and the user is notified once when that conversation is waiting. "
        "For a task that should happen once, use the `once` kind: a cron expression with a date would fire "
        "again every year."
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
        try:
            return croniter(expr, after.astimezone(self._tz)).get_next(datetime)
        except CroniterBadDateError as e:
            # 语法合法但永远不会到期（如 2 月 30 日）：croniter 找不到下一个时间
            raise InvalidInput(f"cron expression {expr!r} never fires: {e}") from e

    def configure(self, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        expr = parse_config(KIND, ScheduleConfig, config).cron
        # 改配置、重新启用都从头算：之前的连续跳过（连同它是否通知过）不带过来
        return {"cron": expr}, {
            "next_run_at": self.next_run(expr, self._clock()).isoformat(),
            "skipped": None,
        }

    def status(self, trigger: Trigger) -> ScheduleStatus:
        streak = _streak(trigger)
        return ScheduleStatus(
            next_run_at=next_run_at(trigger),
            skipped=None if streak is None else SkippedFires.model_validate(streak, from_attributes=True),
        )


def next_run_at(trigger: Trigger) -> datetime:
    return stored_time(trigger, trigger.state.get("next_run_at"), "next_run_at")


def _cron(trigger: Trigger) -> str:
    cron = trigger.config.get("cron")
    if not isinstance(cron, str):
        raise CorruptState(
            f"trigger {trigger.id} ({trigger.name}) has no cron expression: {trigger.config!r}"
        )
    return cron


def _streak(trigger: Trigger) -> _SkipStreak | None:
    raw = trigger.state.get("skipped")
    if raw is None:
        return None
    try:
        return _SkipStreak.model_validate(raw)
    except ValidationError as e:
        raise CorruptState(
            f"trigger {trigger.id} ({trigger.name}) has an invalid skipped state: {raw!r}"
        ) from e


class ScheduleDue:
    """定时触发的到期规则：到点推进到下次时间，上一次触发的会话还没做完（previous_fire_state_in）时
    跳过这一次，并把跳过记下来（见模块说明）。"""

    kind = KIND

    def __init__(self, activity: SessionActivity, kind: ScheduleKind, messaging: Messaging):
        self._activity = activity
        self._kind = kind
        self._messaging = messaging

    def due_at(self, trigger: Trigger) -> datetime:
        return next_run_at(trigger)

    async def consume_in(self, s: AsyncSession, trigger: Trigger, now: datetime) -> str | None:
        set_state(trigger, next_run_at=self._kind.next_run(_cron(trigger), now).isoformat())
        previous = await previous_fire_state_in(s, self._activity, trigger)
        if previous == WorkState.IDLE:
            set_state(trigger, skipped=None)
            return None
        assert trigger.last_session_id is not None  # 从没触发过的是 idle
        streak = _streak(trigger)
        if streak is not None and streak.session_id != trigger.last_session_id:
            streak = None
        notified = streak is not None and streak.notified
        notify = previous == WorkState.WAITING and not notified
        streak = _SkipStreak(
            count=1 if streak is None else streak.count + 1,
            since=now if streak is None else streak.since,
            previous_state=previous,
            session_id=trigger.last_session_id,
            notified=notified or notify,
        )
        if notify:
            self._messaging.send_in(s, self._waiting_notice(trigger, streak.count))
        set_state(trigger, skipped=streak.model_dump(mode="json"))
        return f"previous run still {previous.value} (skipped {streak.count} in a row)"

    @staticmethod
    def _waiting_notice(trigger: Trigger, count: int) -> Notify:
        return Notify(
            agent_id=trigger.agent_id,
            title=f"定时触发器 {trigger.name} 在跳过",
            body=(
                f"上一次触发的会话在等待（后台任务、委派或你的回答），它结束前每次到点都会跳过，已跳过 {count} 次。"
                "点开看它在等什么。"
            ),
            # 点开通知打开挡路的那个会话
            session_id=trigger.last_session_id,
            issue_id=None,
        )
