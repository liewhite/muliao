"""触发器领域的公开接口（HTTP 适配层与组合根用）。

各触发方式的配置与状态类型也在这里导出：HTTP 接口按 kind 把它们组成有区分字段的联合类型。事件的投递目标（router、targets）
是领域内部，对外只有地址格式的说明 ADDRESS_HELP。组合根经 TriggerDomain.build 装配。
"""

from muliao.triggers.assembly import TriggerDomain
from muliao.triggers.faults import FaultSource, TriggerFault
from muliao.triggers.once import OnceConfig, OnceStatus
from muliao.triggers.precheck import Precheck, PrecheckFailed
from muliao.triggers.schedule import ScheduleConfig, ScheduleStatus
from muliao.triggers.service import (
    EVENT_MAX_BYTES,
    SESSION_NOTE,
    Fired,
    TriggerKind,
    TriggerKindName,
    TriggerService,
    TriggerView,
    route_address,
)
from muliao.triggers.stream import StreamConfig, StreamStatus
from muliao.triggers.targets import ADDRESS_HELP
from muliao.triggers.webhook import WebhookConfig, Webhooks, WebhookStatus

__all__ = [
    "ADDRESS_HELP",
    "EVENT_MAX_BYTES",
    "FaultSource",
    "Fired",
    "OnceConfig",
    "OnceStatus",
    "Precheck",
    "PrecheckFailed",
    "SESSION_NOTE",
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
    "WebhookConfig",
    "WebhookStatus",
    "Webhooks",
    "route_address",
]
