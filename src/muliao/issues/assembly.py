"""issue 领域的装配与交给组合根的部件（platform.wiring.Parts）：看板服务、看板工具、订阅，以及 issue 会话在指令里的说明。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar
from zoneinfo import ZoneInfo

from pydantic_ai import FunctionToolset

from muliao.agents.api import AgentService
from muliao.issues.rules import SESSION_NOTE
from muliao.issues.service import IssueService
from muliao.issues.tools import issues_toolset
from muliao.notifications.events import NotifyIn
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tooling import RunScope
from muliao.platform.wiring import Parts
from muliao.sessions.api import Origin, SenderInbox, SessionActivity, StopSession


@dataclass(frozen=True)
class IssueDomain(Parts):
    issues: IssueService
    _tools: FunctionToolset[RunScope]

    # issue 会话在指令里的说明（组合根合成 sessions.api.OriginNotes 交给会话领域）
    origin_notes: ClassVar[Mapping[Origin, str]] = MappingProxyType({Origin.ISSUE: SESSION_NOTE})

    @classmethod
    def build(
        cls,
        *,
        db: Database,
        inbox: SenderInbox,
        notify: NotifyIn,
        tz: ZoneInfo,
        activity: SessionActivity,
        stop_session: StopSession,
        agents: AgentService,
    ) -> IssueDomain:
        issues = IssueService(db, inbox, notify, tz, activity, stop_session)
        return cls(issues=issues, _tools=issues_toolset(issues, agents))

    def toolsets(self) -> Sequence[FunctionToolset[RunScope]]:
        return (self._tools,)

    def subscribe(self, messaging: Messaging) -> None:
        self.issues.subscribe(messaging)
