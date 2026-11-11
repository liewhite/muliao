"""触发器领域的装配：组合根只给出依赖与配置，领域自己决定内部对象怎么接（同 sessions.assembly.SessionDomain）。

触发方式在这里注册：新增一种（如消息队列）= 一个 TriggerKind + 它的激活机制，在 build 里注册，有常驻循环的加进 loops()；
组合根不用改（它只把 loops() 的每个循环放进服务的根任务组）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pydantic_ai import FunctionToolset

from muliao.agents.api import AgentService
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tooling import RunScope
from muliao.sessions.api import SessionActivity
from muliao.triggers.due import DueRunner
from muliao.triggers.faults import Faults
from muliao.triggers.once import OnceDue, OnceKind
from muliao.triggers.precheck import PrecheckRunner
from muliao.triggers.schedule import ScheduleDue, ScheduleKind
from muliao.triggers.service import EVENT_MAX_BYTES, TriggerService
from muliao.triggers.stream import StreamKind, StreamRunner
from muliao.triggers.tools import triggers_toolset
from muliao.triggers.webhook import WebhookKind, Webhooks
from muliao.workspace.api import AgentCommands


@dataclass(frozen=True)
class TriggerDomain:
    # 管理与触发的入口（HTTP 与工具）
    triggers: TriggerService
    # 按到期时间触发的扫描循环（定时、一次性）
    due: DueRunner
    # 流式触发器托管的常驻命令
    streams: StreamRunner
    # webhook 的 HTTP 入口
    webhooks: Webhooks
    _precheck_timeout_seconds: float

    @classmethod
    def build(
        cls,
        *,
        db: Database,
        tz: ZoneInfo,
        messaging: Messaging,
        commands: AgentCommands,
        activity: SessionActivity,
        public_url: str,
        precheck_timeout_seconds: float,
    ) -> TriggerDomain:
        schedule_kind, once_kind = ScheduleKind(tz), OnceKind(tz)
        precheck = PrecheckRunner(commands, precheck_timeout_seconds, EVENT_MAX_BYTES)
        # 脚本出错的记录与告警，触发与流式托管共用
        faults = Faults(messaging, tz)
        triggers = TriggerService(
            db,
            {k.name: k for k in (schedule_kind, once_kind, WebhookKind(public_url), StreamKind())},
            tz,
            commands,
            precheck,
            messaging,
            faults,
            activity,
        )
        return cls(
            triggers=triggers,
            # 按到期时间触发的方式共用一个扫描循环，各自给出到期时间与这次到期被用掉之后这一行怎么变
            due=DueRunner(db, triggers, [ScheduleDue(activity, schedule_kind, messaging), OnceDue()]),
            streams=StreamRunner(db, triggers, activity, commands, messaging, faults, EVENT_MAX_BYTES),
            webhooks=Webhooks(db, triggers),
            _precheck_timeout_seconds=precheck_timeout_seconds,
        )

    def loops(self) -> Sequence[tuple[str, Callable[[], Awaitable[None]]]]:
        """触发器领域的常驻循环（名字，入口）：到期扫描与流式托管，由组合根在服务的根任务组里各自启动。"""
        return (("due", self.due.run_forever), ("streams", self.streams.run_forever))

    def toolset(self, agents: AgentService) -> FunctionToolset[RunScope]:
        """agent 管理触发器的工具。"""
        return triggers_toolset(self.triggers, agents, self._precheck_timeout_seconds)

    def subscribe(self, messaging: Messaging) -> None:
        """触发器领域的订阅：删除 agent 时删掉它的触发器。"""
        self.triggers.subscribe(messaging)
