"""issue 看板：人与 agent 协作的事项（docs/architecture.md「issue」一节）。

每次修改在一个事务里写 issue、记时间线，并按 rules.wake_target 判定是否唤醒被指派的 agent：要唤醒时同一事务发出
定向命令 Receive（来源 Origin.ISSUE，origin_ref 是 issue id），提交后马上投递（run_now）。同一个 agent 在同一个
issue 上续写同一个会话（Issue.sessions）。

actor 是做这件事的 agent id，用户为 None；session_id 是 agent 动手时所在的会话。agent 自己的修改、评论从不唤醒
它自己（不论在哪个会话里做的），否则它的 issue 会话会和动手的会话并发处理同一件事。处理会话（Issue.sessions，巡检
据此判断有没有人在做）只显式登记：agent 在当前会话里给自己建、派、转 todo 的 issue 登记当前会话；在别的会话里接手
用 claim，让出用 release。登记的会话停下后的下一次唤醒（评论、巡检）才开 issue 自己的会话。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentService, active_agent_in, agent_in
from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.issues.rules import (
    USER,
    ActivityView,
    IssueView,
    Snapshot,
    comment_wakes,
    render_issue,
    wake_message,
    wake_target,
)
from muliao.issues.tables import ActivityKind, Issue, IssueActivity, IssuePriority, IssueStatus
from muliao.notifications.events import Notify
from muliao.platform.db import Database, utcnow
from muliao.platform.ids import new_id
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit
from muliao.platform.tooling import Origin
from muliao.sessions.api import (
    Inbound,
    NewSession,
    SessionActivity,
    WorkState,
    check_receivable_in,
    receive_command,
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


def _title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidInput("issue title must not be empty")
    if len(title) > TITLE_MAX:
        raise InvalidInput(f"issue title is longer than {TITLE_MAX} characters")
    return title


def _assignee_of(issue: Issue) -> str | None:
    if issue.assigned_to_user:
        return USER
    return issue.assignee_agent_id


def _working(issue: Issue, actor: str | None, session_id: str | None) -> str | None:
    """这次修改不该唤醒的 agent：被指派人在某个会话里自己动手时就是它（它知道自己做了什么，唤醒只会多开一个并发的
    会话）。不在会话里（没有地方接着做）的修改照常唤醒。"""
    if actor is not None and session_id is not None and actor == issue.assignee_agent_id:
        return actor
    return None


def _snapshot(issue: Issue) -> Snapshot:
    return Snapshot(status=issue.status, assignee=_assignee_of(issue))


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
        """删除 agent 的流程：指派给它、归它负责的 issue 改为未指派、无负责人（记在时间线上），忘掉它的会话。"""

        async def commit(s: AsyncSession) -> None:
            # sessions 是 JSON：按 agent id 粗筛（id 是不含特殊字符的十六进制），再在下面精确判断
            query = select(Issue).where(
                or_(
                    Issue.assignee_agent_id == event.agent_id,
                    Issue.owner_agent_id == event.agent_id,
                    cast(Issue.sessions, String).contains(event.agent_id),
                )
            )
            for issue in (await s.scalars(query)).all():
                if issue.assignee_agent_id == event.agent_id:
                    issue.assignee_agent_id = None
                    self._record(s, issue, None, ActivityKind.ASSIGNEE, change=(event.agent_id, None))
                if issue.owner_agent_id == event.agent_id:
                    issue.owner_agent_id = None
                    self._record(s, issue, None, ActivityKind.OWNER, change=(event.agent_id, None))
                if event.agent_id in issue.sessions:
                    issue.sessions = {k: v for k, v in issue.sessions.items() if k != event.agent_id}

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
        if assignee == USER:
            query = query.where(Issue.assigned_to_user)
        elif assignee is not None:
            query = query.where(Issue.assignee_agent_id == assignee)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def get(self, issue_id: int) -> IssueDetail:
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            return IssueDetail(issue, await self._activities_in(s, issue_id))

    async def work_states(self, issues: list[Issue]) -> dict[int, WorkState | None]:
        """被指派的 agent 在每个 issue 自己的会话里的状态：在跑、在等外面的结果、还是空闲。没指派给 agent、它还没有
        这个 issue 的会话（或会话已删除）时为 None。用来发现「该有人在做、却没人在做」的 issue。"""
        states: dict[int, WorkState | None] = {}
        async with self._db.transaction() as s:
            for issue in issues:
                agent = issue.assignee_agent_id
                session_id = None if agent is None else issue.sessions.get(agent)
                if session_id is None or await session_in(s, session_id) is None:
                    states[issue.id] = None
                else:
                    states[issue.id] = await self._activity.work_state_in(s, session_id)
        return states

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
                assignee_agent_id=None if assignee in (None, USER) else assignee,
                assigned_to_user=assignee == USER,
                creator_agent_id=actor,
                sessions={},
                created_at=now,
                updated_at=now,
            )
            s.add(issue)
            await s.flush()
            self._record(s, issue, actor, ActivityKind.CREATED)
            wake = await self._after_change_in(s, None, issue, actor, session_id)
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
            changed = False
            if title is not None and _title(title) != issue.title:
                self._record(s, issue, actor, ActivityKind.TITLE, change=(issue.title, _title(title)))
                issue.title, changed = _title(title), True
            if description is not None and description != issue.description:
                self._record(s, issue, actor, ActivityKind.DESCRIPTION)
                issue.description, changed = description, True
            if status is not None and status != issue.status:
                self._record(s, issue, actor, ActivityKind.STATUS, change=(issue.status, status))
                issue.status, changed = status, True
            if owner is not None and owner != issue.owner_agent_id:
                await active_agent_in(s, owner)
                self._record(s, issue, actor, ActivityKind.OWNER, change=(issue.owner_agent_id, owner))
                issue.owner_agent_id, changed = owner, True
            if priority is not None and priority != issue.priority:
                self._record(s, issue, actor, ActivityKind.PRIORITY, change=(issue.priority, priority))
                issue.priority, changed = priority, True
            if not isinstance(assignee, Keep) and assignee != before.assignee:
                await self._check_assignee_in(s, assignee)
                self._record(s, issue, actor, ActivityKind.ASSIGNEE, change=(before.assignee, assignee))
                issue.assignee_agent_id = None if assignee in (None, USER) else assignee
                issue.assigned_to_user = assignee == USER
                changed = True
            if not changed:
                return issue
            issue.updated_at = utcnow()
            wake = await self._after_change_in(s, before, issue, actor, session_id)
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
            await s.flush()
            wake = None
            if (agent := comment_wakes(_snapshot(issue), _working(issue, actor, session_id))) is not None:
                who = "The user" if actor is None else f"Agent {await self._name_in(s, actor)}"
                wake = await self._wake_in(s, issue, agent, f"{who} commented on issue #{issue.id}.")
        log.info("issue commented", extra={"issue_id": issue_id, "actor": actor})
        await self._deliver(wake)
        return activity

    async def claim(self, actor: str, issue_id: int, session_id: str) -> None:
        """agent 在当前会话里接手指派给它的 issue：把这个会话登记为处理会话（巡检看它在不在做事）。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            if issue.assignee_agent_id != actor:
                raise Conflict(f"issue #{issue_id} is not assigned to you; only the assignee can claim it")
            if issue.sessions.get(actor) == session_id:
                return
            issue.sessions = {**issue.sessions, actor: session_id}
            self._record(s, issue, actor, ActivityKind.CLAIMED)
            issue.updated_at = utcnow()
        log.info("issue claimed", extra={"issue_id": issue_id, "agent": actor, "session_id": session_id})

    async def release(self, actor: str, issue_id: int, session_id: str) -> bool:
        """agent 让出 issue：断开登记的处理会话（必须就是它现在所在的会话）。issue 仍是 todo / 进行中、指派给它时，
        马上开 issue 自己的新会话接手（返回 True）；否则只断开，等下次被唤醒。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            if issue.sessions.get(actor) != session_id:
                raise Conflict(
                    f"this conversation is not the one working on issue #{issue_id}; nothing to release"
                )
            issue.sessions = {k: v for k, v in issue.sessions.items() if k != actor}
            self._record(s, issue, actor, ActivityKind.RELEASED)
            issue.updated_at = utcnow()
            wake = None
            if wake_target(None, _snapshot(issue), None) == actor:
                wake = await self._wake_in(
                    s,
                    issue,
                    actor,
                    f"Issue #{issue.id} was released by the conversation working on it; take it over.",
                )
        log.info(
            "issue released", extra={"issue_id": issue_id, "agent": actor, "handed_over": wake is not None}
        )
        await self._deliver(wake)
        return wake is not None

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
    ) -> Task | None:
        after = _snapshot(issue)
        if actor is not None and after.assignee == USER and (before is None or before.assignee != USER):
            # agent 把 issue 交给用户：推送通知，点开是它在这个 issue 上的会话
            self._messaging.send_in(
                s,
                Notify(
                    agent_id=actor,
                    title=f"#{issue.id} 指派给你：{issue.title}"[:200],
                    body=f"状态 {issue.status}",
                    session_id=issue.sessions.get(actor),
                ),
            )
        if session_id is not None and actor is not None and wake_target(before, after, None) == actor:
            # agent 在当前会话里给自己建、派、转 todo 的 issue：就在这个会话里做，登记它为处理会话，不另开会话。
            # 这个会话停下而 issue 还没做完时，下一次唤醒（评论、巡检）再开 issue 自己的会话（见 _wake_in）
            issue.sessions = {**issue.sessions, actor: session_id}
            log.info(
                "issue adopted by the session that assigned it", extra={"issue_id": issue.id, "agent": actor}
            )
        agent = wake_target(before, after, _working(issue, actor, session_id))
        if agent is None:
            return None
        if before is None:
            reason = f"Issue #{issue.id} was created and assigned to you."
        elif before.assignee != agent:
            reason = f"Issue #{issue.id} was assigned to you."
        else:
            reason = f"Issue #{issue.id} moved from {before.status} to {issue.status}."
        who = "the user" if actor is None else await self._name_in(s, actor)
        return await self._wake_in(s, issue, agent, f"{reason} (by {who})")

    async def _wake_in(self, s: AsyncSession, issue: Issue, agent_id: str, reason: str) -> Task:
        """在调用方的事务里给 agent 投一封信：续写它在这个 issue 上的会话，没有（或已删除）就新开。"""
        view = await self._view_in(s, issue)
        existing = issue.sessions.get(agent_id)
        if existing is not None:
            info = await session_in(s, existing)
            # 只续写这个 issue 自己开的会话；登记的处理会话可能是 agent 的别的会话（如用户的对话），不往里投信
            if info is None or (info.origin, info.origin_ref) != (Origin.ISSUE, str(issue.id)):
                existing = None
        session: str | NewSession = (
            existing
            if existing is not None
            else NewSession(agent_id=agent_id, title=f"#{issue.id} {issue.title}"[:200], id=new_id())
        )
        inbound = Inbound(
            origin=Origin.ISSUE,
            origin_ref=str(issue.id),
            content=wake_message(reason, render_issue(view)),
            session=session,
            awaited=False,
        )
        await check_receivable_in(s, inbound)
        delivery = self._messaging.send_in(s, receive_command(inbound))
        target = session if isinstance(session, str) else session.id
        if target is None:
            raise RuntimeError("a new issue session must have a preassigned id")
        if existing is None:
            issue.sessions = {**issue.sessions, agent_id: target}
        log.info("issue wakes agent", extra={"issue_id": issue.id, "agent": agent_id, "session_id": target})
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
            assignee=await assignee(_assignee_of(issue)),
            owner="none" if issue.owner_agent_id is None else await name(issue.owner_agent_id),
            creator=await who(issue.creator_agent_id),
            activities=views,
        )
