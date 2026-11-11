"""触发器领域的装配：组合根只给出依赖与配置，领域自己决定内部对象怎么接，并声明交给组合根的部件（platform.wiring.Parts）。

触发方式在这里注册：新增一种（如消息队列）= 一个 TriggerKind + 它的激活机制，在 build 里注册，有常驻循环的加进 loops()；
组合根不用改。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar
from zoneinfo import ZoneInfo

from pydantic_ai import FunctionToolset

from muliao.agents.api import AgentService
from muliao.agents.events import AgentDeleting
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tooling import RunScope
from muliao.platform.wiring import Loop, Parts
from muliao.sessions.api import Origin, SessionActivity
from muliao.triggers.due import DueRunner
from muliao.triggers.faults import Faults
from muliao.triggers.once import OnceDue, OnceKind
from muliao.triggers.precheck import PrecheckRunner
from muliao.triggers.schedule import ScheduleDue, ScheduleKind
from muliao.triggers.service import EVENT_MAX_BYTES, SESSION_NOTE, TriggerService
from muliao.triggers.stream import StreamKind, StreamRunner
from muliao.triggers.tools import triggers_toolset
from muliao.workspace.api import AgentCommands


@dataclass(frozen=True)
class TriggerDomain(Parts):
    # 管理与触发的入口（HTTP 与工具）
    triggers: TriggerService
    # 按到期时间触发的扫描循环（定时、一次性）
    due: DueRunner
    # 流式触发器托管的常驻命令
    streams: StreamRunner
    _tools: FunctionToolset[RunScope]

    @classmethod
    def build(
        cls,
        *,
        db: Database,
        tz: ZoneInfo,
        messaging: Messaging,
        commands: AgentCommands,
        activity: SessionActivity,
        agents: AgentService,
        precheck_timeout_seconds: float,
    ) -> TriggerDomain:
        schedule_kind, once_kind = ScheduleKind(tz), OnceKind(tz)
        precheck = PrecheckRunner(commands, precheck_timeout_seconds, EVENT_MAX_BYTES)
        # 脚本出错的记录与告警，触发与流式托管共用
        faults = Faults(messaging, tz)
        triggers = TriggerService(
            db,
            {k.name: k for k in (schedule_kind, once_kind, StreamKind())},
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
            _tools=triggers_toolset(triggers, agents, precheck_timeout_seconds),
        )

    # 触发器开出的会话在指令里的说明（组合根合成 sessions.api.OriginNotes 交给会话领域）
    origin_notes: ClassVar[Mapping[Origin, str]] = MappingProxyType({Origin.TRIGGER: SESSION_NOTE})

    def toolsets(self) -> Sequence[FunctionToolset[RunScope]]:
        return (self._tools,)

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentDeleting, "triggers.agent_deleting", self.triggers.on_agent_deleting)

    def loops(self) -> Sequence[tuple[str, Loop]]:
        return (("due", self.due.run_forever), ("streams", self.streams.run_forever))
