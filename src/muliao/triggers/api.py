"""触发器领域的公开接口（HTTP 适配层与组合根用）。

各触发方式的配置与状态类型也在这里导出：HTTP 接口按 kind 把它们组成有区分字段的联合类型。
组合根经 TriggerDomain.build 装配。
"""

from muliao.triggers.assembly import TriggerDomain
from muliao.triggers.faults import FaultSource, TriggerFault
from muliao.triggers.once import OnceConfig, OnceStatus
from muliao.triggers.precheck import Precheck, PrecheckFailed
from muliao.triggers.schedule import ScheduleConfig, ScheduleStatus
from muliao.triggers.service import (
    EVENT_MAX_BYTES,
    Fired,
    TriggerKind,
    TriggerKindName,
    TriggerService,
    TriggerView,
)
from muliao.triggers.stream import StreamConfig, StreamStatus

__all__ = [
    "EVENT_MAX_BYTES",
    "FaultSource",
    "Fired",
    "OnceConfig",
    "OnceStatus",
    "Precheck",
    "PrecheckFailed",
    "ScheduleConfig",
    "ScheduleStatus",
    "StreamConfig",
    "StreamStatus",
    "TriggerDomain",
    "TriggerFault",
    "TriggerKind",
    "TriggerKindName",
    "TriggerService",
    "TriggerView",
]
