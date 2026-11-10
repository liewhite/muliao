"""触发器脚本出错（触发器领域内部）：前置检查失败、事件内容里写错的 `@route` 地址、流式命令反复退出。

这几种都是触发器所属 agent 写的脚本坏了，记录、清除、告警的规则只在这里写一次：
- 记录（report_in）：存在触发器的 faults 列，同一（来源, 地址）只留一条，原因取最新；只有新出现的那条才告警，
  所以每分钟检查一次的触发器坏了不会每分钟告警，同一个坏地址反复出现也只告警一次。
- 清除：来源自己的恢复信号清本来源（recovered：检查成功、流式命令稳定运行；写错的地址没有恢复信号——丢不丢取决于
  内容，好坏批次常常交替）；人工干预（换前置检查、改配置、重新启用）清全部（reset，TriggerService.update）。
- 告警去向（alert_in）：交给触发器所属的 agent 开一个新会话，由它修脚本，自己解决不了时通知用户；这封信投不进去
  （agent 已归档、正在删除）时退回给用户推送，不静默吞掉。
faults 列只在这里赋值（tests/test_boundaries.py）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import DomainError
from muliao.notifications.events import Notify
from muliao.platform.db import utcnow
from muliao.platform.ids import new_id
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import Clock
from muliao.platform.values import local_time
from muliao.sessions.api import NewConversation, Origin, SenderMail, receive_command_in
from muliao.triggers.tables import Trigger

log = logging.getLogger(__name__)


class FaultSource(StrEnum):
    """哪个脚本出的错。"""

    PRECHECK = "precheck"
    ROUTE = "route"
    STREAM = "stream"


# 告警会话的标题（跟在触发器名后面）
_TITLES = {
    FaultSource.PRECHECK: "前置检查出错",
    FaultSource.ROUTE: "投递地址写错",
    FaultSource.STREAM: "流式命令反复退出",
}


class TriggerFault(BaseModel):
    """一条出错：来源、原因；写错的投递地址还带着那个地址（每个地址各一条）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source: FaultSource
    error: str
    address: str | None = None


_FAULTS = TypeAdapter(list[TriggerFault])


def faults_of(trigger: Trigger) -> list[TriggerFault]:
    return _FAULTS.validate_python(trigger.faults)


def _store(trigger: Trigger, faults: list[TriggerFault]) -> None:
    # JSON 列只在整体赋值时被追踪
    trigger.faults = _FAULTS.dump_python(faults, mode="json")


def record(trigger: Trigger, faults: Sequence[TriggerFault]) -> list[TriggerFault]:
    """记下这些出错：同一（来源, 地址）只留一条、原因取最新；返回其中新出现的（要告警的）。"""
    stored = faults_of(trigger)
    known = {(f.source, f.address) for f in stored}
    latest = {(f.source, f.address): f for f in faults}
    new = [f for key, f in latest.items() if key not in known]
    _store(trigger, [latest.pop((f.source, f.address), f) for f in stored] + list(latest.values()))
    return new


def recovered(trigger: Trigger, source: FaultSource) -> None:
    """这个来源恢复了（检查成功、流式命令稳定运行）：清掉它的出错，再坏时重新告警。"""
    faults = faults_of(trigger)
    kept = [f for f in faults if f.source != source]
    if len(kept) != len(faults):
        log.info("trigger %s recovered", source, extra={"trigger": trigger.name})
        _store(trigger, kept)


def reset(trigger: Trigger) -> None:
    """人工干预（换前置检查、改配置、重新启用）：旧的错不再作数，全部清掉，再出错重新告警。"""
    _store(trigger, [])


class Faults:
    def __init__(self, messaging: Messaging, tz: ZoneInfo, clock: Clock = utcnow):
        self._messaging = messaging
        self._tz = tz
        self._clock = clock

    async def report_in(
        self,
        s: AsyncSession,
        trigger: Trigger,
        faults: Sequence[TriggerFault],
        letter: Callable[[list[TriggerFault]], str],
    ) -> None:
        """在调用方事务里记下这些出错（同一来源）；其中有新出现的，用 letter(新出现的) 写信告警一次。"""
        if not faults:
            return
        [source] = {f.source for f in faults}
        new = record(trigger, faults)
        log.warning(
            "trigger %s failed", source, extra={"trigger": trigger.name, "faults": [f.error for f in faults]}
        )
        if new:
            await self.alert_in(
                s, trigger, title=_TITLES[source], letter=letter(new), summary="\n".join(f.error for f in new)
            )

    async def alert_in(
        self, s: AsyncSession, trigger: Trigger, *, title: str, letter: str, summary: str
    ) -> None:
        """告诉触发器所属的 agent（新会话，letter 说明出了什么错、怎么处理）；投不进去时退回给用户推送（title 与 summary）。"""
        inbound = SenderMail(
            origin=Origin.TRIGGER,
            origin_ref=str(trigger.id),
            content=letter,
            session=NewConversation(
                id=new_id(),
                agent_id=trigger.agent_id,
                title=f"{trigger.name} {title} · {local_time(self._clock(), self._tz)}",
            ),
        )
        try:
            command = await receive_command_in(s, inbound)
        except DomainError as e:
            log.warning(
                "trigger alert could not reach the agent, notifying the user: %s",
                e,
                extra={"trigger": trigger.name},
            )
            self._messaging.send_in(
                s,
                Notify(
                    agent_id=None,
                    title=f"触发器 {trigger.name} {title}",
                    body=summary[:500],
                    session_id=None,
                    issue_id=None,
                ),
            )
            return
        self._messaging.send_in(s, command)
