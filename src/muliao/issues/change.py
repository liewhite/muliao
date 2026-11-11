"""issue 的变更单元：一次修改带出的全部后果（记动态、投递、通知、放行下游）只在这里发生。

修改入口（IssueService 的各个命令、事件处理）只描述改了什么：在调用方的事务里 `async with changes.open_in(...)` 取到
issue 行，期间只经单元改字段（`set_*` / `apply`）、记动态（`record`）；正常退出时统一结算：

- 投递与通知：按构造时声明的 `Routing` —— `BY_RULES` 按 rules.recipient 判定投给谁（IssueDelivery），`Direct(agent)`
  直接投给这个 agent（issue_stop 的收尾消息、请停滞的 agent 表态的 Unsettled），`RECORD_ONLY` 只记时间线（运行失败、删除 agent 的清理）；
- 放行下游：这次的状态变更让 issue 进入 done / cancelled（rules.finishing）时，每个下游各开一个单元记一条
  DependencyFinished，按同一条规则投递——不再等别的上游时，这就是让它开工的那封信。

出异常时不结算（事务随之回滚）。一次修改什么都没改（没记动态）时什么都不发。Issue 的字段只在本模块赋值（新建也是
`new_issue_in`）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import runnable_agent_in
from muliao.errors import InvalidInput, NotFound
from muliao.issues.activity import (
    Activity,
    AssigneeChanged,
    Change,
    DependencyChange,
    DependencyFinished,
    DependsOnChanged,
    DescriptionEdited,
    Happening,
    OwnerChanged,
    PriorityChanged,
    StatusChanged,
    TitleChanged,
    stored,
)
from muliao.issues.delivery import IssueDelivery
from muliao.issues.graph import downstream_in, set_depends_on_in
from muliao.issues.rules import agent_assignee, finishing
from muliao.issues.tables import Issue, IssueActivity, IssuePriority, IssueStatus
from muliao.platform.db import utcnow
from muliao.platform.messaging import Messaging
from muliao.platform.values import KEEP, Keep

log = logging.getLogger(__name__)

TITLE_MAX = 300


@dataclass(frozen=True)
class ByRules:
    """按 rules.recipient 判定投给谁。"""


@dataclass(frozen=True)
class Direct:
    """直接投给这个 agent，不按 rules.recipient 判定：issue_stop 的收尾消息投给被停下的 agent，不论 issue 的新状态；
    运行结束后请停滞的 agent 表态（Unsettled），它记在那个 issue 会话名下，按「自己会话里的动作不投回」会被挡掉。"""

    agent: str


@dataclass(frozen=True)
class RecordOnly:
    """只记时间线：不投递、不通知、不放行下游。"""


type Routing = ByRules | Direct | RecordOnly
BY_RULES = ByRules()
RECORD_ONLY = RecordOnly()


def checked_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidInput("issue title must not be empty")
    if len(title) > TITLE_MAX:
        raise InvalidInput(f"issue title is longer than {TITLE_MAX} characters")
    return title


async def _check_assignee_in(s: AsyncSession, assignee: str | None) -> None:
    agent = agent_assignee(assignee)
    if agent is not None:
        # 正在删除的 agent 按不存在处理：否则删除流程清理过之后又指派给它，没人认领；归档的拒绝（它接不到信）
        await runnable_agent_in(s, agent)


async def new_issue_in(
    s: AsyncSession,
    *,
    owner: str,
    title: str,
    description: str,
    status: IssueStatus,
    priority: IssuePriority,
    assignee: str | None,
    creator: str,
    created_at: datetime,
) -> Issue:
    """校验后建一行 issue 加进事务（还没有编号；记 Created 与投递在之后各自的单元里）。"""
    await _check_assignee_in(s, assignee)
    await runnable_agent_in(s, owner)
    issue = Issue(
        owner_agent_id=owner,
        title=checked_title(title),
        description=description,
        status=status,
        priority=priority,
        assignee=assignee,
        creator=creator,
        created_at=created_at,
    )
    s.add(issue)
    return issue


class IssueChange:
    """一次修改：一个 issue、做事的人（actor 与他所在的会话）、这期间记下的动态。由 IssueChanges.open_in 给出。"""

    def __init__(self, s: AsyncSession, issue: Issue, actor: str, session_id: str | None):
        self._s = s
        self._issue = issue
        self.actor = actor
        self.session_id = session_id
        self._recorded: list[IssueActivity] = []

    @property
    def changed(self) -> bool:
        """这次修改记下了动态（有字段真的变了，或记了评论等）。"""
        return bool(self._recorded)

    @property
    def issue(self) -> Issue:
        """当前的 issue 行（只读：改字段经下面的方法）。"""
        return self._issue

    def record(self, what: Happening) -> IssueActivity:
        """记一条动态（返回那一行，提交后可用 Activity.of 读回）。"""
        kind, body, change = stored(what)
        row = IssueActivity(
            issue_id=self._issue.id,
            created_at=utcnow(),
            actor=self.actor,
            kind=kind,
            body=body,
            change=change,
            session_id=self.session_id,
        )
        self._s.add(row)
        self._recorded.append(row)
        # 时间线上多一条就是 issue 变了：updated_at 只在这里更新
        self._issue.updated_at = row.created_at
        return row

    # 每个 set_* 只在值真的变了时改字段、记一条动态

    def set_title(self, title: str) -> None:
        title = checked_title(title)
        if title != self._issue.title:
            self.record(TitleChanged(change=Change(before=self._issue.title, to=title)))
            self._issue.title = title

    def set_description(self, description: str) -> None:
        if description != self._issue.description:
            self.record(DescriptionEdited())
            self._issue.description = description

    async def set_depends_on(self, depends_on: Sequence[int]) -> None:
        replaced = await set_depends_on_in(self._s, self._issue, depends_on)
        if replaced is not None:
            self.record(DependsOnChanged(change=Change(before=replaced[0], to=replaced[1])))

    def set_status(self, status: IssueStatus) -> None:
        if status != self._issue.status:
            self.record(StatusChanged(change=Change(before=self._issue.status, to=status)))
            self._issue.status = status

    async def set_owner(self, owner: str | None) -> None:
        """None 只用于系统清理（负责的 agent 被删除）。"""
        if owner != self._issue.owner_agent_id:
            if owner is not None:
                await runnable_agent_in(self._s, owner)
            self.record(OwnerChanged(change=Change(before=self._issue.owner_agent_id, to=owner)))
            self._issue.owner_agent_id = owner

    def set_priority(self, priority: IssuePriority) -> None:
        if priority != self._issue.priority:
            self.record(PriorityChanged(change=Change(before=self._issue.priority, to=priority)))
            self._issue.priority = priority

    async def set_assignee(self, assignee: str | None) -> None:
        if assignee != self._issue.assignee:
            await _check_assignee_in(self._s, assignee)
            self.record(AssigneeChanged(change=Change(before=self._issue.assignee, to=assignee)))
            self._issue.assignee = assignee

    async def apply(
        self,
        *,
        owner: str | None = None,
        title: str | None = None,
        description: str | None = None,
        status: IssueStatus | None = None,
        priority: IssuePriority | None = None,
        assignee: str | None | Keep = KEEP,
        depends_on: Sequence[int] | None = None,
    ) -> None:
        """改给出的字段（None 不变；assignee 为 KEEP 不变、None 取消指派；depends_on 整体替换），时间线上的顺序固定。"""
        if title is not None:
            self.set_title(title)
        if description is not None:
            self.set_description(description)
        if depends_on is not None:
            await self.set_depends_on(depends_on)
        if status is not None:
            self.set_status(status)
        if owner is not None:
            await self.set_owner(owner)
        if priority is not None:
            self.set_priority(priority)
        if not isinstance(assignee, Keep):
            await self.set_assignee(assignee)


class IssueChanges:
    """开变更单元、在退出时结算（见模块说明）。"""

    def __init__(self, messaging: Messaging, tz: ZoneInfo):
        self._delivery = IssueDelivery(messaging, tz)

    @asynccontextmanager
    async def open_in(
        self,
        s: AsyncSession,
        issue_id: int,
        actor: str,
        session_id: str | None,
        routing: Routing = BY_RULES,
    ) -> AsyncIterator[IssueChange]:
        """在调用方的事务里取 issue 行（不存在报 NotFound），开一次修改；正常退出时按 routing 结算。"""
        issue = await s.get(Issue, issue_id)
        if issue is None:
            raise NotFound(f"issue #{issue_id} not found")
        change = IssueChange(s, issue, actor, session_id)
        yield change
        await self._settle_in(s, change, routing)

    async def _settle_in(self, s: AsyncSession, change: IssueChange, routing: Routing) -> None:
        if not change.changed or isinstance(routing, RecordOnly):
            return
        await s.flush()
        happened = [Activity.of(row) for row in change._recorded]
        issue, actor, session_id = change.issue, change.actor, change.session_id
        to = routing.agent if isinstance(routing, Direct) else None
        await self._delivery.post_in(s, issue, actor, session_id, happened, to=to)
        if (was := finishing([a.what for a in happened])) is not None:
            downstream = await downstream_in(s, issue.id)
            for down in downstream:
                async with self.open_in(s, down, actor, session_id) as d:
                    d.record(
                        DependencyFinished(
                            change=DependencyChange(issue=issue.id, before=was, to=issue.status)
                        )
                    )
            if downstream:
                log.info("issue unblocked downstream", extra={"issue_id": issue.id, "status": issue.status})
