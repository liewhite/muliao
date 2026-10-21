"""issue 看板：人与 agent 协作的事项（docs/architecture.md「issue 看板」一节）。

每次修改在一个事务里写 issue、记时间线，并按 rules.recipient 判定这次的动态投给谁：投给被指派的 agent 时，同一事务
发出定向命令 Receive，提交后马上投递（run_now）。信投进这个 agent 在这个 issue 上唯一的会话：会话领域按发送方
（Origin.ISSUE，origin_ref 为 issue id）与 agent 定位，没有就新建（sessions.api.SessionKey）；这里不存会话 id。

actor 是做这件事的 agent id，用户为 None；session_id 是 agent 动手时所在的会话。它在自己这个 issue 会话里的动作
不投回那个会话（避免自激）；issue 只在它自己的会话里做，普通对话只建、派 issue。

依赖（issue_dependencies，有向无环）：还在等上游的 issue 不投、不算停滞（rules.recipient / rules.stalled 看
Snapshot.waiting_on）。上游进入 done / cancelled 时，在同一事务里给每个下游记一条 DEPENDENCY 动态并按同一条规则
投递：下游因此不再等待，那封信就投出去；还在等别的上游就只记时间线。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentNames, active_agent_in, agent_names_in
from muliao.agents.events import AgentDeleting
from muliao.errors import InvalidInput, NotFound
from muliao.issues.rules import (
    FINISHED,
    USER,
    ActivityView,
    Dependency,
    IssueView,
    Snapshot,
    cycle_through,
    delivery_message,
    recipient,
    render_issue,
    stalled,
    waiting_on,
)
from muliao.issues.tables import (
    ActivityKind,
    Issue,
    IssueActivity,
    IssueDependency,
    IssuePriority,
    IssueStatus,
)
from muliao.notifications.events import Notify
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit
from muliao.platform.values import KEEP, Keep, local_time
from muliao.sessions.api import (
    Inbound,
    Origin,
    SessionActivity,
    SessionKey,
    WorkState,
    keyed_sessions_among,
    receive_command_in,
    session_in,
)

log = logging.getLogger(__name__)

TITLE_MAX = 300


@dataclass(frozen=True)
class IssueDetail:
    issue: Issue
    activities: list[IssueActivity]


@dataclass(frozen=True)
class Links:
    """issue 在依赖图里的位置：它依赖的上游、依赖它的下游（都按 id 排序）。"""

    depends_on: list[Dependency]
    blocks: list[Dependency]


@dataclass(frozen=True)
class IssueWork:
    """谁在这个 issue 上做事、它在等谁（从会话领域与依赖图派生，不存）。"""

    # 每个 agent 在这个 issue 上的会话：agent id -> 会话 id
    sessions: dict[str, str]
    # 被指派 agent 的 issue 会话的工作状态；没指派给 agent 或它还没有这个会话时为 None
    assignee_session: WorkState | None
    # 该有人做却没人在做（rules.stalled）
    stalled: bool
    # 上游、下游，与还没完成的上游（rules.waiting_on）
    depends_on: list[Dependency]
    blocks: list[Dependency]
    waiting_on: tuple[int, ...]


def _title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidInput("issue title must not be empty")
    if len(title) > TITLE_MAX:
        raise InvalidInput(f"issue title is longer than {TITLE_MAX} characters")
    return title


def _snapshot(issue: Issue, links: Links) -> Snapshot:
    return Snapshot(status=issue.status, assignee=issue.assignee, waiting_on=waiting_on(links.depends_on))


def _ids(found: Sequence[Dependency]) -> list[int]:
    return [d.id for d in found]


class IssueService:
    def __init__(
        self,
        db: Database,
        messaging: Messaging,
        tz: ZoneInfo,
        run_now: Callable[[int], Awaitable[None]],
        activity: SessionActivity,
    ):
        self._activity = activity
        self._db = db
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
        """每个 issue 上谁在做事：各 agent 的 issue 会话、被指派 agent 的会话在跑 / 在等外面的结果 / 空闲，是否停滞，
        以及它的上游、下游与还在等的上游。查询次数与 issue 数无关。"""
        async with self._db.transaction() as s:
            keyed = await keyed_sessions_among(s, Origin.ISSUE, [str(i.id) for i in issues])
            keys = {i.id: (str(i.id), i.assignee) for i in issues if i.assignee not in (None, USER)}
            states = await self._activity.keyed_work_states_in(s, Origin.ISSUE, list(keys.values()))
            links = await self._links_in(s, [i.id for i in issues])
        work = {}
        for i in issues:
            state = states[keys[i.id]] if i.id in keys else None
            snapshot = _snapshot(i, links[i.id])
            work[i.id] = IssueWork(
                sessions={agent: info.id for agent, info in keyed[str(i.id)].items()},
                assignee_session=state,
                stalled=stalled(snapshot, state),
                depends_on=links[i.id].depends_on,
                blocks=links[i.id].blocks,
                waiting_on=snapshot.waiting_on,
            )
        return work

    async def render(self, issue_id: int) -> str:
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            view = await self._view_in(s, issue, (await self._links_in(s, [issue_id]))[issue_id])
        return render_issue(view)

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
        depends_on: Sequence[int] = (),
    ) -> Issue:
        """depends_on：它依赖的上游 issue；不能成环。"""
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
            await self._set_depends_on_in(s, issue, depends_on)
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
        depends_on: Sequence[int] | None = None,
    ) -> Issue:
        """只改给出的字段（None 不变；assignee 为 KEEP 不变、None 取消指派；depends_on 整体替换）。每个真正变了的
        字段记一条时间线。issue 进入 done / cancelled 时通知它的下游（_unblock_in）。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            before = issue.status, issue.assignee
            happened: list[IssueActivity] = []

            def record(kind: ActivityKind, change: tuple[Any, Any] | None = None) -> None:
                happened.append(self._record(s, issue, actor, kind, change=change))

            if title is not None and _title(title) != issue.title:
                record(ActivityKind.TITLE, (issue.title, _title(title)))
                issue.title = _title(title)
            if description is not None and description != issue.description:
                record(ActivityKind.DESCRIPTION)
                issue.description = description
            if depends_on is not None:
                replaced = await self._set_depends_on_in(s, issue, depends_on)
                if replaced is not None:
                    record(ActivityKind.DEPENDS_ON, replaced)
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
            wake = [await self._after_change_in(s, before[1], issue, actor, session_id, happened)]
            if before[0] not in FINISHED and issue.status in FINISHED:
                wake += await self._unblock_in(s, issue, before[0], actor, session_id)
        log.info("issue updated", extra={"issue_id": issue_id, "actor": actor, "status": issue.status})
        await self._deliver(*wake)
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
        """删除 issue（用户的操作）。依赖它的下游少了这个上游：各记一条 DEPENDS_ON 动态，按规则投递（可能因此不再等待）。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            downstream = _ids((await self._links_in(s, [issue_id]))[issue_id].blocks)
            before = await self._links_in(s, downstream)
            await s.delete(issue)
            await s.flush()
            wake = []
            for down_id in downstream:
                down = await self._get_in(s, down_id)
                old = _ids(before[down_id].depends_on)
                change = (old, [i for i in old if i != issue_id])
                activity = self._record(s, down, None, ActivityKind.DEPENDS_ON, change=change)
                down.updated_at = utcnow()
                wake.append(await self._deliver_in(s, down, None, None, [activity]))
        log.info("issue deleted", extra={"issue_id": issue_id, "downstream": downstream})
        await self._deliver(*wake)

    # ---- 内部 ----

    async def _get_in(self, s: AsyncSession, issue_id: int) -> Issue:
        issue = await s.get(Issue, issue_id)
        if issue is None:
            raise NotFound(f"issue #{issue_id} not found")
        return issue

    async def _links_in(self, s: AsyncSession, issue_ids: list[int]) -> dict[int, Links]:
        """一批 issue 的上游与下游（带对方的当前状态）：两次查询，与 issue 数无关。"""
        links = {i: Links(depends_on=[], blocks=[]) for i in issue_ids}
        upstream = (
            select(IssueDependency.issue_id, Issue.id, Issue.status)
            .join(Issue, Issue.id == IssueDependency.depends_on_id)
            .where(IssueDependency.issue_id.in_(issue_ids))
            .order_by(Issue.id)
        )
        for down, up, status in (await s.execute(upstream)).all():
            links[down].depends_on.append(Dependency(id=up, status=status))
        downstream = (
            select(IssueDependency.depends_on_id, Issue.id, Issue.status)
            .join(Issue, Issue.id == IssueDependency.issue_id)
            .where(IssueDependency.depends_on_id.in_(issue_ids))
            .order_by(Issue.id)
        )
        for up, down, status in (await s.execute(downstream)).all():
            links[up].blocks.append(Dependency(id=down, status=status))
        return links

    async def _set_depends_on_in(
        self, s: AsyncSession, issue: Issue, depends_on: Sequence[int]
    ) -> tuple[list[int], list[int]] | None:
        """把 issue 的上游整体换成 depends_on：上游必须存在、不能重复、不能成环。返回（原来的, 现在的），没变为 None。"""
        wanted = sorted(set(depends_on))
        if len(wanted) != len(depends_on):
            raise InvalidInput(f"depends_on lists an issue more than once: {list(depends_on)}")
        existing = set(await s.scalars(select(Issue.id).where(Issue.id.in_(wanted))))
        missing = [f"#{up}" for up in wanted if up not in existing]
        if missing:
            raise InvalidInput(f"issue #{issue.id} cannot depend on {', '.join(missing)}: not found")
        edges = [
            (down, up)
            for down, up in await s.execute(select(IssueDependency.issue_id, IssueDependency.depends_on_id))
        ]
        current = sorted(up for down, up in edges if down == issue.id)
        if current == wanted:
            return None
        cycle = cycle_through(issue.id, wanted, edges)
        if cycle is not None:
            path = " -> ".join(f"#{i}" for i in cycle)
            raise InvalidInput(f"dependencies must not form a cycle: {path} (each depends on the next)")
        await s.execute(delete(IssueDependency).where(IssueDependency.issue_id == issue.id))
        s.add_all(IssueDependency(issue_id=issue.id, depends_on_id=up) for up in wanted)
        await s.flush()
        return current, wanted

    async def _unblock_in(
        self, s: AsyncSession, issue: Issue, was: IssueStatus, actor: str | None, session_id: str | None
    ) -> list[Task | None]:
        """issue 进入 done / cancelled：每个下游记一条 DEPENDENCY 动态，按 rules.recipient 投递——不再等别的上游时，
        这就是让它开工的那封信。"""
        wake = []
        for down in _ids((await self._links_in(s, [issue.id]))[issue.id].blocks):
            downstream = await self._get_in(s, down)
            activity = self._record(
                s, downstream, actor, ActivityKind.DEPENDENCY, change=(was, issue.status), upstream=issue.id
            )
            downstream.updated_at = utcnow()
            wake.append(await self._deliver_in(s, downstream, actor, session_id, [activity]))
        if wake:
            log.info("issue unblocked downstream", extra={"issue_id": issue.id, "status": issue.status})
        return wake

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
        upstream: int | None = None,
    ) -> IssueActivity:
        recorded = None if change is None else {"from": change[0], "to": change[1]}
        if upstream is not None:
            recorded = {"issue": upstream, **(recorded or {})}
        activity = IssueActivity(
            issue_id=issue.id,
            created_at=utcnow(),
            actor_agent_id=actor,
            kind=kind,
            body=body,
            change=recorded,
        )
        s.add(activity)
        return activity

    async def _after_change_in(
        self,
        s: AsyncSession,
        assignee_before: str | None,
        issue: Issue,
        actor: str | None,
        session_id: str | None,
        happened: list[IssueActivity],
    ) -> Task | None:
        if actor is not None and issue.assignee == USER and assignee_before != USER:
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
        links = (await self._links_in(s, [issue.id]))[issue.id]
        agent = recipient(
            _snapshot(issue, links), await self._from_its_session_in(s, issue, actor, session_id)
        )
        if agent is None:
            return None
        # 信里只有摘要和这次的动态：只渲染 happened，不读整条时间线
        letter = self._view_with(issue, happened, links, await agent_names_in(s))
        inbound = Inbound(
            origin=Origin.ISSUE,
            origin_ref=str(issue.id),
            content=delivery_message(letter),
            session=SessionKey(agent_id=agent, title=f"#{issue.id} {issue.title}"[:200]),
            awaited=False,
        )
        delivery = self._messaging.send_in(s, await receive_command_in(s, inbound))
        log.info("issue activity delivered", extra={"issue_id": issue.id, "agent": agent, "actor": actor})
        return delivery

    async def _deliver(self, *wake: Task | None) -> None:
        for task in wake:
            if task is not None:
                await self._run_now(task.id)

    async def _view_in(self, s: AsyncSession, issue: Issue, links: Links) -> IssueView:
        await s.flush()
        return self._view_with(issue, await self._activities_in(s, issue.id), links, await agent_names_in(s))

    def _view_with(
        self, issue: Issue, activities: list[IssueActivity], links: Links, names: AgentNames
    ) -> IssueView:
        def who(agent_id: str | None) -> str:
            return USER if agent_id is None else names[agent_id]

        def assignee(value: Any) -> str:
            if value is None:
                return "unassigned"
            return USER if value == USER else names[value]

        def issues(ids: list[int]) -> str:
            return ", ".join(f"#{i}" for i in ids) or "none"

        views = []
        for a in activities:
            before = after = None
            if a.change is not None:
                if a.kind in (ActivityKind.ASSIGNEE, ActivityKind.OWNER):
                    before, after = assignee(a.change["from"]), assignee(a.change["to"])
                elif a.kind == ActivityKind.DEPENDS_ON:
                    before, after = issues(a.change["from"]), issues(a.change["to"])
                else:
                    before, after = str(a.change["from"]), str(a.change["to"])
            views.append(
                ActivityView(
                    at=local_time(a.created_at, self._tz),
                    actor=who(a.actor_agent_id),
                    kind=a.kind,
                    body=a.body,
                    before=before,
                    after=after,
                    upstream=a.change["issue"] if a.kind == ActivityKind.DEPENDENCY else None,
                )
            )
        return IssueView(
            id=issue.id,
            title=issue.title,
            description=issue.description,
            status=issue.status,
            priority=issue.priority,
            assignee=assignee(issue.assignee),
            owner="none" if issue.owner_agent_id is None else names[issue.owner_agent_id],
            creator=who(issue.creator_agent_id),
            depends_on=links.depends_on,
            blocks=links.blocks,
            activities=views,
        )
