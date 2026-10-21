"""issue 看板：人与 agent 协作的事项（docs/architecture.md「issue 看板」一节）。

每次修改在一个事务里写 issue、记时间线，并按 rules.recipient 判定这次的动态投给谁：投给被指派的 agent 时，同一事务
发出定向命令 Receive，提交后马上投递（run_now）。信投进这个 agent 在这个 issue 上唯一的会话：会话领域按发送方
（Origin.ISSUE，origin_ref 为 issue id）与 agent 定位，没有就新建（sessions.api.SessionKey）；这里不存会话 id。

actor 是做这件事的 agent id，用户为 None；session_id 是 agent 动手时所在的会话。它在自己这个 issue 会话里的动作
不投回那个会话（避免自激）；issue 只在它自己的会话里做，普通对话只建、派 issue。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentService, active_agent_in, agent_in
from muliao.agents.events import AgentDeleting
from muliao.errors import InvalidInput, NotFound
from muliao.issues.rules import (
    USER,
    ActivityView,
    IssueView,
    Snapshot,
    delivery_message,
    recipient,
    render_issue,
    stalled,
)
from muliao.issues.tables import ActivityKind, Issue, IssueActivity, IssuePriority, IssueStatus
from muliao.notifications.events import Notify
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit
from muliao.platform.tooling import Origin
from muliao.sessions.api import (
    Inbound,
    SessionActivity,
    SessionKey,
    WorkState,
    keyed_sessions_among,
    receive_command_in,
    session_in,
)

log = logging.getLogger(__name__)

TITLE_MAX = 300


class Keep(Enum):
    """局部修改里「不改这个字段」，用于本身可以设为 None 的字段（指派人）。"""

    KEEP = "keep"


KEEP = Keep.KEEP


@dataclass(frozen=True)
class IssueDetail:
    issue: Issue
    activities: list[IssueActivity]


@dataclass(frozen=True)
class IssueWork:
    """谁在这个 issue 上做事（从会话领域派生，不存）。"""

    # 每个 agent 在这个 issue 上的会话：agent id -> 会话 id
    sessions: dict[str, str]
    # 被指派 agent 的 issue 会话的工作状态；没指派给 agent 或它还没有这个会话时为 None
    assignee_session: WorkState | None
    # 该有人做却没人在做（rules.stalled）
    stalled: bool


def _title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidInput("issue title must not be empty")
    if len(title) > TITLE_MAX:
        raise InvalidInput(f"issue title is longer than {TITLE_MAX} characters")
    return title


def _snapshot(issue: Issue) -> Snapshot:
    return Snapshot(status=issue.status, assignee=issue.assignee)


class IssueService:
    def __init__(
        self,
        db: Database,
        agents: AgentService,
        messaging: Messaging,
        tz: ZoneInfo,
        run_now: Callable[[int], Awaitable[None]],
        activity: SessionActivity,
    ):
        self._activity = activity
        self._db = db
        self._agents = agents
        self._messaging = messaging
        self._tz = tz
        # 马上执行一个已提交的投递任务（Worker.run_now）
        self._run_now = run_now

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentDeleting, "issues.agent_deleting", self.on_agent_deleting)

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：指派给它、归它负责的 issue 改为未指派、无负责人（记在时间线上）。它的会话由会话领域删除。"""

        async def commit(s: AsyncSession) -> None:
            query = select(Issue).where(
                or_(Issue.assignee == event.agent_id, Issue.owner_agent_id == event.agent_id)
            )
            for issue in (await s.scalars(query)).all():
                if issue.assignee == event.agent_id:
                    issue.assignee = None
                    self._record(s, issue, None, ActivityKind.ASSIGNEE, change=(event.agent_id, None))
                if issue.owner_agent_id == event.agent_id:
                    issue.owner_agent_id = None
                    self._record(s, issue, None, ActivityKind.OWNER, change=(event.agent_id, None))

        return commit

    # ---- 查询 ----

    async def list(
        self,
        *,
        statuses: list[IssueStatus] | None = None,
        assignee: str | None = None,
        owner: str | None = None,
    ) -> list[Issue]:
        query = select(Issue).order_by(Issue.updated_at.desc())
        if owner is not None:
            query = query.where(Issue.owner_agent_id == owner)
        if statuses:
            query = query.where(Issue.status.in_(statuses))
        if assignee is not None:
            query = query.where(Issue.assignee == assignee)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def get(self, issue_id: int) -> IssueDetail:
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            return IssueDetail(issue, await self._activities_in(s, issue_id))

    async def work(self, issues: list[Issue]) -> dict[int, IssueWork]:
        """每个 issue 上谁在做事：各 agent 的 issue 会话、被指派 agent 的会话在跑 / 在等外面的结果 / 空闲，以及是否停滞。
        查询次数与 issue 数无关。"""
        async with self._db.transaction() as s:
            keyed = await keyed_sessions_among(s, Origin.ISSUE, [str(i.id) for i in issues])
            keys = {i.id: (str(i.id), i.assignee) for i in issues if i.assignee not in (None, USER)}
            states = await self._activity.keyed_work_states_in(s, Origin.ISSUE, list(keys.values()))
        work = {}
        for i in issues:
            state = states[keys[i.id]] if i.id in keys else None
            work[i.id] = IssueWork(
                sessions={agent: info.id for agent, info in keyed[str(i.id)].items()},
                assignee_session=state,
                stalled=stalled(_snapshot(i), state),
            )
        return work

    async def render(self, issue_id: int) -> str:
        detail = await self.get(issue_id)
        return render_issue(await self._view(detail))

    # ---- 修改 ----

    async def create(
        self,
        actor: str | None,
        *,
        session_id: str | None = None,
        owner: str,
        title: str,
        description: str = "",
        status: IssueStatus = IssueStatus.BACKLOG,
        priority: IssuePriority = IssuePriority.NONE,
        assignee: str | None = None,
    ) -> Issue:
        now = utcnow()
        async with self._db.transaction() as s:
            await self._check_assignee_in(s, assignee)
            await active_agent_in(s, owner)
            issue = Issue(
                owner_agent_id=owner,
                title=_title(title),
                description=description,
                status=status,
                priority=priority,
                assignee=assignee,
                creator_agent_id=actor,
                created_at=now,
                updated_at=now,
            )
            s.add(issue)
            await s.flush()
            created = self._record(s, issue, actor, ActivityKind.CREATED)
            wake = await self._after_change_in(s, None, issue, actor, session_id, [created])
        log.info("issue created", extra={"issue_id": issue.id, "actor": actor, "assignee": assignee})
        await self._deliver(wake)
        return issue

    async def update(
        self,
        actor: str | None,
        issue_id: int,
        *,
        session_id: str | None = None,
        owner: str | None = None,
        title: str | None = None,
        description: str | None = None,
        status: IssueStatus | None = None,
        priority: IssuePriority | None = None,
        assignee: str | None | Keep = KEEP,
    ) -> Issue:
        """只改给出的字段（None 不变；assignee 为 KEEP 不变、None 取消指派）。每个真正变了的字段记一条时间线。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            before = _snapshot(issue)
            happened: list[IssueActivity] = []

            def record(kind: ActivityKind, change: tuple[Any, Any] | None = None) -> None:
                happened.append(self._record(s, issue, actor, kind, change=change))

            if title is not None and _title(title) != issue.title:
                record(ActivityKind.TITLE, (issue.title, _title(title)))
                issue.title = _title(title)
            if description is not None and description != issue.description:
                record(ActivityKind.DESCRIPTION)
                issue.description = description
            if status is not None and status != issue.status:
                record(ActivityKind.STATUS, (issue.status, status))
                issue.status = status
            if owner is not None and owner != issue.owner_agent_id:
                await active_agent_in(s, owner)
                record(ActivityKind.OWNER, (issue.owner_agent_id, owner))
                issue.owner_agent_id = owner
            if priority is not None and priority != issue.priority:
                record(ActivityKind.PRIORITY, (issue.priority, priority))
                issue.priority = priority
            if not isinstance(assignee, Keep) and assignee != issue.assignee:
                await self._check_assignee_in(s, assignee)
                record(ActivityKind.ASSIGNEE, (issue.assignee, assignee))
                issue.assignee = assignee
            if not happened:
                return issue
            issue.updated_at = utcnow()
            wake = await self._after_change_in(s, before, issue, actor, session_id, happened)
        log.info("issue updated", extra={"issue_id": issue_id, "actor": actor, "status": issue.status})
        await self._deliver(wake)
        return issue

    async def comment(
        self, actor: str | None, issue_id: int, body: str, *, session_id: str | None = None
    ) -> IssueActivity:
        if not body.strip():
            raise InvalidInput("comment must not be empty")
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            activity = self._record(s, issue, actor, ActivityKind.COMMENT, body=body)
            issue.updated_at = utcnow()
            wake = await self._deliver_in(s, issue, actor, session_id, [activity])
        log.info("issue commented", extra={"issue_id": issue_id, "actor": actor})
        await self._deliver(wake)
        return activity

    async def delete(self, issue_id: int) -> None:
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            await s.delete(issue)
        log.info("issue deleted", extra={"issue_id": issue_id})

    # ---- 内部 ----

    async def _get_in(self, s: AsyncSession, issue_id: int) -> Issue:
        issue = await s.get(Issue, issue_id)
        if issue is None:
            raise NotFound(f"issue #{issue_id} not found")
        return issue

    async def _activities_in(self, s: AsyncSession, issue_id: int) -> list[IssueActivity]:
        query = select(IssueActivity).where(IssueActivity.issue_id == issue_id).order_by(IssueActivity.id)
        return list((await s.scalars(query)).all())

    async def _check_assignee_in(self, s: AsyncSession, assignee: str | None) -> None:
        if assignee is not None and assignee != USER:
            # 正在删除的 agent 按不存在处理：否则删除流程清理过之后又指派给它，没人认领
            await active_agent_in(s, assignee)

    def _record(
        self,
        s: AsyncSession,
        issue: Issue,
        actor: str | None,
        kind: ActivityKind,
        *,
        body: str | None = None,
        change: tuple[Any, Any] | None = None,
    ) -> IssueActivity:
        activity = IssueActivity(
            issue_id=issue.id,
            created_at=utcnow(),
            actor_agent_id=actor,
            kind=kind,
            body=body,
            change=None if change is None else {"from": change[0], "to": change[1]},
        )
        s.add(activity)
        return activity

    async def _name_in(self, s: AsyncSession, agent_id: str) -> str:
        try:
            return (await agent_in(s, agent_id)).name
        except NotFound:
            return f"(deleted agent {agent_id})"

    async def _after_change_in(
        self,
        s: AsyncSession,
        before: Snapshot | None,
        issue: Issue,
        actor: str | None,
        session_id: str | None,
        happened: list[IssueActivity],
    ) -> Task | None:
        if actor is not None and issue.assignee == USER and (before is None or before.assignee != USER):
            # agent 把 issue 交给用户：推送通知，点开是它交出 issue 的那个会话
            self._messaging.send_in(
                s,
                Notify(
                    agent_id=actor,
                    title=f"#{issue.id} 指派给你：{issue.title}"[:200],
                    body=f"状态 {issue.status}",
                    session_id=session_id,
                ),
            )
        return await self._deliver_in(s, issue, actor, session_id, happened)

    async def _from_its_session_in(
        self, s: AsyncSession, issue: Issue, actor: str | None, session_id: str | None
    ) -> str | None:
        """动作是不是 agent 在它自己这个 issue 会话里做的：是则返回它（这次动态不投回那里）。"""
        if actor is None or session_id is None:
            return None
        info = await session_in(s, session_id)
        if (
            info is None
            or info.agent_id != actor
            or (info.origin, info.origin_ref) != (Origin.ISSUE, str(issue.id))
        ):
            return None
        return actor

    async def _deliver_in(
        self,
        s: AsyncSession,
        issue: Issue,
        actor: str | None,
        session_id: str | None,
        happened: list[IssueActivity],
    ) -> Task | None:
        """在调用方的事务里把这次的动态投给该收到的 agent（rules.recipient），进它在这个 issue 上唯一的会话。"""
        agent = recipient(_snapshot(issue), await self._from_its_session_in(s, issue, actor, session_id))
        if agent is None:
            return None
        view = await self._view_in(s, issue)
        news = view.activities[-len(happened) :]
        inbound = Inbound(
            origin=Origin.ISSUE,
            origin_ref=str(issue.id),
            content=delivery_message(issue.id, news, render_issue(view)),
            session=SessionKey(agent_id=agent, title=f"#{issue.id} {issue.title}"[:200]),
            awaited=False,
        )
        delivery = self._messaging.send_in(s, await receive_command_in(s, inbound))
        log.info("issue activity delivered", extra={"issue_id": issue.id, "agent": agent, "actor": actor})
        return delivery

    async def _deliver(self, wake: Task | None) -> None:
        if wake is not None:
            await self._run_now(wake.id)

    def _local(self, at: datetime) -> str:
        return at.astimezone(self._tz).strftime("%Y-%m-%d %H:%M")

    async def _view_in(self, s: AsyncSession, issue: Issue) -> IssueView:
        await s.flush()
        return await self._view_with(
            issue, await self._activities_in(s, issue.id), lambda a: self._name_in(s, a)
        )

    async def _view(self, detail: IssueDetail) -> IssueView:
        names = {a.id: a.name for a in await self._agents.list()}

        async def name(agent_id: str) -> str:
            return names.get(agent_id, f"(deleted agent {agent_id})")

        return await self._view_with(detail.issue, detail.activities, name)

    async def _view_with(
        self, issue: Issue, activities: list[IssueActivity], name: Callable[[str], Awaitable[str]]
    ) -> IssueView:
        async def who(agent_id: str | None) -> str:
            return "user" if agent_id is None else await name(agent_id)

        async def assignee(value: Any) -> str:
            if value is None:
                return "unassigned"
            return "user" if value == USER else await name(value)

        views = []
        for a in activities:
            before = after = None
            if a.change is not None:
                if a.kind in (ActivityKind.ASSIGNEE, ActivityKind.OWNER):
                    before, after = await assignee(a.change["from"]), await assignee(a.change["to"])
                else:
                    before, after = str(a.change["from"]), str(a.change["to"])
            views.append(
                ActivityView(
                    at=self._local(a.created_at),
                    actor=await who(a.actor_agent_id),
                    kind=a.kind,
                    body=a.body,
                    before=before,
                    after=after,
                )
            )
        return IssueView(
            id=issue.id,
            title=issue.title,
            description=issue.description,
            status=issue.status,
            priority=issue.priority,
            assignee=await assignee(issue.assignee),
            owner="none" if issue.owner_agent_id is None else await name(issue.owner_agent_id),
            creator=await who(issue.creator_agent_id),
            activities=views,
        )
