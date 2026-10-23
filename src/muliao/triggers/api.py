"""触发器领域的公开接口（HTTP 适配层用）。

各触发方式的配置与状态类型也在这里导出：HTTP 接口按 kind 把它们组成有区分字段的联合类型。
"""

from muliao.triggers.once import OnceConfig, OnceStatus
from muliao.triggers.precheck import Precheck, PrecheckFailed
from muliao.triggers.schedule import ScheduleConfig, ScheduleStatus
from muliao.triggers.service import EVENT_MAX_BYTES, TriggerKind, TriggerService
from muliao.triggers.stream import StreamConfig, StreamStatus
from muliao.triggers.webhook import WebhookConfig, Webhooks, WebhookStatus

__all__ = [
    "EVENT_MAX_BYTES",
    "OnceConfig",
    "OnceStatus",
    "Precheck",
    "PrecheckFailed",
    "ScheduleConfig",
    "ScheduleStatus",
    "StreamConfig",
    "StreamStatus",
    "TriggerKind",
    "TriggerService",
    "WebhookConfig",
    "WebhookStatus",
    "Webhooks",
]
