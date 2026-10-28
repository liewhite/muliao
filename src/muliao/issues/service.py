"""issue 看板：人与 agent 协作的事项（docs/architecture.md「issue 看板」一节）。

每次修改在一个事务里写 issue、记时间线，并交给 delivery 在同一事务里投递这次的动态（投给谁见 rules.recipient），
提交后马上执行。依赖图在 graph，给模型看的文本在 render，这里编排它们。

actor 是做这件事的 agent id，用户为 USER，系统自己的清理（删除 agent）为 rules.SYSTEM_ACTOR；session_id 是 agent 动手时所在的会话。它在自己这个 issue 会话里的动作
不投回那个会话（避免自激）；issue 只在它自己的会话里做，普通对话只建、派 issue。

用户的评论在按上述规则没有 agent 接手时投给负责项目的 agent（owner），见 rules.recipient。

依赖（graph）：还在等上游的 issue 不投、不算停滞（rules.recipient / rules.stalled 看 Snapshot.waiting_on）。上游进入
done / cancelled 时，在同一事务里给每个下游记一条 DEPENDENCY 动态并按同一条规则投递：下游因此不再等待，那封信
就投出去；还在等别的上游就只记时间线。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import agent_names_in, runnable_agent_in
from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.issues.delivery import IssueDelivery
from muliao.issues.graph import (
    Dependency,
    batch_label,
    check_batch,
    downstream_in,
    ids_of,
    links_in,
    set_depends_on_in,
    snapshot,
)
from muliao.issues.render import issue_view, render_issue
from muliao.issues.rules import (
    SYSTEM_ACTOR,
    USER,
    agent_assignee,
    ask_to_settle,
    finished,
    handed_by,
    reviewable,
    stalled,
)
from muliao.issues.tables import (
    ActivityKind,
    Issue,
    IssueActivity,
    IssuePriority,
    IssueStatus,
)
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Commit, no_commit
from muliao.platform.values import KEEP, Keep
from muliao.sessions.api import Origin, SessionActivity, WorkState, keyed_sessions_among
from muliao.sessions.events import RunEnded, RunFailed

log = logging.getLogger(__name__)

TITLE_MAX = 300


@dataclass(frozen=True)
class IssueDetail:
    issue: Issue
    activities: list[IssueActivity]


@dataclass(frozen=True)
class IssueWork:
    """谁在这个 issue 上做事、它在等谁（从会话领域与依赖图派生，不存）。"""

    # 每个 agent 在这个 issue 上的会话：agent id -> 会话 id
    sessions: dict[str, str]
    # 被指派 agent 的 issue 会话的工作状态；没指派给 agent 或它还没有这个会话时为 None
    assignee_state: WorkState | None
    # 该有人做却没人在做（rules.stalled）
    stalled: bool
    # 上游、下游，与还没完成的上游（graph.waiting_on）
    depends_on: list[Dependency]
    blocks: list[Dependency]
    waiting_on: tuple[int, ...]
    # 已结束（rules.finished）、用户能审核（rules.reviewable）
    finished: bool
    reviewable: bool


def _title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidInput("issue title must not be empty")
    if len(title) > TITLE_MAX:
        raise InvalidInput(f"issue title is longer than {TITLE_MAX} characters")
    return title


def _check_handed_to_user(issue: Issue) -> None:
    """审核只对当前指派给用户、还没结束（不在 FINISHED）的 issue：已经审过、又被改派或取消（连点、两台设备、通知
    过时）时拒绝，不覆盖别人刚做的事。"""
    if reviewable(issue.status, issue.assignee):
        return
    if issue.assignee != USER:
        raise Conflict(
            f"issue #{issue.id} is not assigned to you (assignee: {issue.assignee}), nothing to review"
        )
    raise Conflict(f"issue #{issue.id} is already {issue.status}, nothing to review")


@dataclass(frozen=True)
class NewIssue:
    """create_many 的一项。depends_on 里的整数是已有 issue 的编号，字符串是同批另一项的 key。"""

    owner: str
    title: str
    description: str = ""
    status: IssueStatus = IssueStatus.BACKLOG
    priority: IssuePriority = IssuePriority.NONE
    assignee: str | None = None
    depends_on: tuple[int | str, ...] = ()
    key: str | None = None


@contextmanager
def batch_item(size: int, n: int, key: str | None) -> Iterator[None]:
    """一批（size 项）里第 n 项的错误带上它是哪一项；只建一个时原样抛出。工具层解析 agent 名时也用它。"""
    try:
        yield
    except (InvalidInput, NotFound) as e:
        if size == 1:
            raise
        raise type(e)(f"{batch_label(n, key)}: {e}") from None


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
        self._tz = tz
        self._delivery = IssueDelivery(messaging, tz, run_now)

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentDeleting, "issues.agent_deleting", self.on_agent_deleting)
        messaging.subscribe(RunFailed, "issues.run_failed", self.on_run_failed)
        messaging.subscribe(RunEnded, "issues.run_ended", self.on_run_ended)

    async def on_run_ended(self, event: RunEnded, _delivery: Delivery) -> Commit:
        """issue 会话里的一次运行结束了：issue 指派给这个 agent、却因此停滞（还是 in_progress，会话空闲）时，以它的名义记一条
        UNSETTLED 并投进那个会话，请它表态（rules.ask_to_settle）。停在挂起的调用上、被用户停止的不问；会话已经又有事要做
        （信箱里还有信、在等后台任务）时不算停滞，不问。其他来源的会话、已被删除的 issue 不处理。"""
        if event.origin != Origin.ISSUE or event.origin_ref is None or event.stopped or event.paused:
            return no_commit
        issue_id = int(event.origin_ref)

        async def commit(s: AsyncSession) -> None:
            issue = await s.get(Issue, issue_id)
            if issue is None:
                return
            ref = str(issue_id)
            current = (await keyed_sessions_among(s, Origin.ISSUE, [ref]))[ref].get(event.agent_id)
            if current is None or current.id != event.session_id:
                # 事件迟到（人工重试）时会话已被删除或换了新的：那次运行已不是它在这个 issue 上的会话，不问
                return
            key = (ref, event.agent_id)
            state = (await self._activity.keyed_work_states_in(s, Origin.ISSUE, [key]))[key]
            snap = snapshot(issue, (await links_in(s, [issue_id]))[issue_id])
            if not ask_to_settle(snap, event.agent_id, state):
                return
            activity = self._record(s, issue, event.agent_id, ActivityKind.UNSETTLED)
            issue.updated_at = utcnow()
            # 投递任务随这个事务提交，由 worker 照常执行（订阅方的事务里没有「提交后马上执行」）
            await self._delivery.deliver_in(s, issue, event.agent_id, None, [activity])
            log.info("issue left unsettled, asked", extra={"issue_id": issue_id, "run_id": event.run_id})

        return commit

    async def on_run_failed(self, event: RunFailed, _delivery: Delivery) -> Commit:
        """issue 会话里的运行最终失败：在 issue 时间线上以该 agent 的名义记一笔（失败的是它的运行）。通知用户由通知领域
        订阅 RunFailed 做，这里不重复发。不投递这条动态、不改状态：投进会话就是自动重试，永久性的错误会陪它无限循环；
        之后有新动态时照常投递、唤醒它。其他来源的会话、已被删除的 issue 不处理。"""
        if event.origin != Origin.ISSUE or event.origin_ref is None:
            return no_commit
        issue_id = int(event.origin_ref)

        async def commit(s: AsyncSession) -> None:
            issue = await s.get(Issue, issue_id)
            if issue is None:
                log.info(
                    "run failed on a deleted issue", extra={"issue_id": issue_id, "run_id": event.run_id}
                )
                return
            self._record(s, issue, event.agent_id, ActivityKind.RUN_FAILED, body=event.summary)
            issue.updated_at = utcnow()
            log.info("issue run failure recorded", extra={"issue_id": issue_id, "run_id": event.run_id})

        return commit

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：指派给它、归它负责的 issue 改为未指派、无负责人（以系统身份记在时间线上）。它的会话由会话
        领域删除。"""

        async def commit(s: AsyncSession) -> None:
            query = select(Issue).where(
                or_(Issue.assignee == event.agent_id, Issue.owner_agent_id == event.agent_id)
            )
            for issue in (await s.scalars(query)).all():
                if issue.assignee == event.agent_id:
                    issue.assignee = None
                    self._record(s, issue, SYSTEM_ACTOR, ActivityKind.ASSIGNEE, change=(event.agent_id, None))
                if issue.owner_agent_id == event.agent_id:
                    issue.owner_agent_id = None
                    self._record(s, issue, SYSTEM_ACTOR, ActivityKind.OWNER, change=(event.agent_id, None))
                issue.updated_at = utcnow()

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
            keys = {
                i.id: (str(i.id), agent) for i in issues if (agent := agent_assignee(i.assignee)) is not None
            }
            states = await self._activity.keyed_work_states_in(s, Origin.ISSUE, list(keys.values()))
            links = await links_in(s, [i.id for i in issues])
        work = {}
        for i in issues:
            state = states[keys[i.id]] if i.id in keys else None
            snap = snapshot(i, links[i.id])
            work[i.id] = IssueWork(
                sessions={agent: info.id for agent, info in keyed[str(i.id)].items()},
                assignee_state=state,
                stalled=stalled(snap, state),
                depends_on=links[i.id].depends_on,
                blocks=links[i.id].blocks,
                waiting_on=snap.waiting_on,
                finished=finished(i.status),
                reviewable=reviewable(i.status, i.assignee),
            )
        return work

    async def render(self, issue_id: int) -> str:
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            activities = await self._activities_in(s, issue_id)
            links = (await links_in(s, [issue_id]))[issue_id]
            view = issue_view(issue, activities, links, await agent_names_in(s), self._tz)
        return render_issue(view)

    # ---- 修改 ----

    async def create(
        self,
        actor: str,
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
        new = NewIssue(
            owner=owner,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee=assignee,
            depends_on=tuple(depends_on),
        )
        return (await self.create_many(actor, [new], session_id=session_id))[0]

    async def create_many(
        self, actor: str, new: Sequence[NewIssue], *, session_id: str | None = None
    ) -> list[Issue]:
        """一次建一批 issue（按给出的顺序编号），可以互相依赖：depends_on 里的字符串是本批另一项的 key。整批一个
        事务，任何一项不合法整批不建；全部建好、依赖连好之后才算各自的投递，提交后再投：先开工的子 issue 看得到
        同批的父 issue（blocks），也不会在依赖补上之前就被投递。"""
        if not new:
            raise InvalidInput("no issues to create")
        check_batch([(item.key, item.depends_on) for item in new])
        now = utcnow()
        async with self._db.transaction() as s:
            created: list[Issue] = []
            for n, item in enumerate(new):
                with batch_item(len(new), n, item.key):
                    await self._check_assignee_in(s, item.assignee)
                    await runnable_agent_in(s, item.owner)
                    issue = Issue(
                        owner_agent_id=item.owner,
                        title=_title(item.title),
                        description=item.description,
                        status=item.status,
                        priority=item.priority,
                        assignee=item.assignee,
                        creator=actor,
                        created_at=now,
                        updated_at=now,
                    )
                s.add(issue)
                created.append(issue)
            await s.flush()
            ids = {
                item.key: issue.id for item, issue in zip(new, created, strict=True) if item.key is not None
            }
            for n, (item, issue) in enumerate(zip(new, created, strict=True)):
                upstream = [ids[d] if isinstance(d, str) else d for d in item.depends_on]
                with batch_item(len(new), n, item.key):
                    await set_depends_on_in(s, issue, upstream)
            wake = []
            for issue in created:
                happened = [self._record(s, issue, actor, ActivityKind.CREATED)]
                wake.append(
                    await self._delivery.notify_and_deliver_in(s, None, issue, actor, session_id, happened)
                )
        for issue in created:
            log.info(
                "issue created", extra={"issue_id": issue.id, "actor": actor, "assignee": issue.assignee}
            )
        await self._delivery.run(*wake)
        return created

    async def update(
        self,
        actor: str,
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
            happened = await self._apply_in(
                s,
                issue,
                actor,
                owner=owner,
                title=title,
                description=description,
                status=status,
                priority=priority,
                assignee=assignee,
                depends_on=depends_on,
            )
            if not happened:
                return issue
            wake = await self._changed_in(s, issue, before, actor, session_id, happened)
        log.info("issue updated", extra={"issue_id": issue_id, "actor": actor, "status": issue.status})
        await self._delivery.run(*wake)
        return issue

    async def approve(self, issue_id: int) -> Issue:
        """用户审核通过交给自己的 issue：设为 done（有下游时照常放行）。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            _check_handed_to_user(issue)
            before = issue.status, issue.assignee
            happened = await self._apply_in(s, issue, USER, status=IssueStatus.DONE)
            if not happened:
                return issue
            wake = await self._changed_in(s, issue, before, USER, None, happened)
        log.info("issue approved", extra={"issue_id": issue_id})
        await self._delivery.run(*wake)
        return issue

    async def reject(self, issue_id: int, reason: str) -> Issue:
        """用户打回交给自己的 issue：理由记成评论，回到 in_progress，指派给把它交给用户的那个 agent（rules.handed_by）。
        三条动态一起投进那个 agent 的 issue 会话（一封信）。"""
        if not reason.strip():
            raise InvalidInput("the reason for sending an issue back must not be empty")
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            _check_handed_to_user(issue)
            activities = await self._activities_in(s, issue_id)
            assigners = [a.actor for a in activities if a.kind == ActivityKind.ASSIGNEE]
            agent = handed_by(issue.creator, assigners)
            if agent is None:
                raise Conflict(
                    f"issue #{issue_id} was not handed to you by an agent: assign it to an agent yourself"
                )
            try:
                await runnable_agent_in(s, agent)
            except NotFound:
                raise Conflict(
                    f"the agent that handed issue #{issue_id} to you no longer exists: "
                    "assign it to an agent yourself"
                ) from None
            before = issue.status, issue.assignee
            happened = [self._record(s, issue, USER, ActivityKind.COMMENT, body=reason)]
            happened += await self._apply_in(s, issue, USER, status=IssueStatus.IN_PROGRESS, assignee=agent)
            wake = await self._changed_in(s, issue, before, USER, None, happened)
        log.info("issue sent back", extra={"issue_id": issue_id, "assignee": agent})
        await self._delivery.run(*wake)
        return issue

    async def _apply_in(
        self,
        s: AsyncSession,
        issue: Issue,
        actor: str,
        *,
        owner: str | None = None,
        title: str | None = None,
        description: str | None = None,
        status: IssueStatus | None = None,
        priority: IssuePriority | None = None,
        assignee: str | None | Keep = KEEP,
        depends_on: Sequence[int] | None = None,
    ) -> list[IssueActivity]:
        """改 issue 的字段（取值规则同 update），每个真正变了的字段记一条时间线，返回这些动态。"""
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
            replaced = await set_depends_on_in(s, issue, depends_on)
            if replaced is not None:
                record(ActivityKind.DEPENDS_ON, replaced)
        if status is not None and status != issue.status:
            record(ActivityKind.STATUS, (issue.status, status))
            issue.status = status
        if owner is not None and owner != issue.owner_agent_id:
            await runnable_agent_in(s, owner)
            record(ActivityKind.OWNER, (issue.owner_agent_id, owner))
            issue.owner_agent_id = owner
        if priority is not None and priority != issue.priority:
            record(ActivityKind.PRIORITY, (issue.priority, priority))
            issue.priority = priority
        if not isinstance(assignee, Keep) and assignee != issue.assignee:
            await self._check_assignee_in(s, assignee)
            record(ActivityKind.ASSIGNEE, (issue.assignee, assignee))
            issue.assignee = assignee
        return happened

    async def _changed_in(
        self,
        s: AsyncSession,
        issue: Issue,
        before: tuple[IssueStatus, str | None],
        actor: str,
        session_id: str | None,
        happened: list[IssueActivity],
    ) -> list[Task | None]:
        """issue 变了（happened 非空）之后：更新时间，投递这些动态；从（改动前的状态, 指派人）进入 done / cancelled 时
        通知下游。返回要马上执行的投递。"""
        issue.updated_at = utcnow()
        wake = [await self._delivery.notify_and_deliver_in(s, before[1], issue, actor, session_id, happened)]
        if not finished(before[0]) and finished(issue.status):
            wake += await self._unblock_in(s, issue, before[0], actor, session_id)
        return wake

    async def comment(
        self, actor: str, issue_id: int, body: str, *, session_id: str | None = None
    ) -> IssueActivity:
        if not body.strip():
            raise InvalidInput("comment must not be empty")
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            activity = self._record(s, issue, actor, ActivityKind.COMMENT, body=body)
            issue.updated_at = utcnow()
            wake = await self._delivery.deliver_in(s, issue, actor, session_id, [activity])
        log.info("issue commented", extra={"issue_id": issue_id, "actor": actor})
        await self._delivery.run(wake)
        return activity

    async def delete(self, issue_id: int) -> None:
        """删除 issue（用户的操作）。依赖它的下游少了这个上游：各记一条 DEPENDS_ON 动态，按规则投递（可能因此不再等待）。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            downstream = await downstream_in(s, issue_id)
            before = await links_in(s, downstream)
            await s.delete(issue)
            await s.flush()
            wake = []
            for down_id in downstream:
                down = await self._get_in(s, down_id)
                old = ids_of(before[down_id].depends_on)
                change = (old, [i for i in old if i != issue_id])
                happened = [self._record(s, down, USER, ActivityKind.DEPENDS_ON, change=change)]
                down.updated_at = utcnow()
                wake.append(await self._delivery.deliver_in(s, down, USER, None, happened))
        log.info("issue deleted", extra={"issue_id": issue_id, "downstream": downstream})
        await self._delivery.run(*wake)

    # ---- 内部 ----

    async def _get_in(self, s: AsyncSession, issue_id: int) -> Issue:
        issue = await s.get(Issue, issue_id)
        if issue is None:
            raise NotFound(f"issue #{issue_id} not found")
        return issue

    async def _unblock_in(
        self, s: AsyncSession, issue: Issue, was: IssueStatus, actor: str, session_id: str | None
    ) -> list[Task | None]:
        """issue 进入 done / cancelled：每个下游记一条 DEPENDENCY 动态，按 rules.recipient 投递——不再等别的上游时，
        这就是让它开工的那封信。"""
        wake = []
        for down in await downstream_in(s, issue.id):
            downstream = await self._get_in(s, down)
            happened = [
                self._record(
                    s,
                    downstream,
                    actor,
                    ActivityKind.DEPENDENCY,
                    change=(was, issue.status),
                    upstream=issue.id,
                )
            ]
            downstream.updated_at = utcnow()
            wake.append(await self._delivery.deliver_in(s, downstream, actor, session_id, happened))
        if wake:
            log.info("issue unblocked downstream", extra={"issue_id": issue.id, "status": issue.status})
        return wake

    async def _activities_in(self, s: AsyncSession, issue_id: int) -> list[IssueActivity]:
        query = select(IssueActivity).where(IssueActivity.issue_id == issue_id).order_by(IssueActivity.id)
        return list((await s.scalars(query)).all())

    async def _check_assignee_in(self, s: AsyncSession, assignee: str | None) -> None:
        agent = agent_assignee(assignee)
        if agent is not None:
            # 正在删除的 agent 按不存在处理：否则删除流程清理过之后又指派给它，没人认领；归档的拒绝（它接不到信）
            await runnable_agent_in(s, agent)

    def _record(
        self,
        s: AsyncSession,
        issue: Issue,
        actor: str,
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
            actor=actor,
            kind=kind,
            body=body,
            change=recorded,
        )
        s.add(activity)
        return activity
