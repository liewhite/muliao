"""issue 看板：人与 agent 协作的事项（docs/architecture.md「issue 看板」一节）。

每次修改在一个事务里写 issue、记时间线（_record，同时更新 updated_at），再把这次的动态交给 _post_in：它从动态本身
推出要发的通知、投给谁（rules.recipient）、要不要放行下游（rules.finishing），在同一事务里发出，提交后马上执行。
依赖图在 graph，给模型看的文本在 render，这里编排它们。

actor 是做这件事的 agent id，用户为 USER，系统自己的清理（删除 agent）为 rules.SYSTEM_ACTOR；session_id 是 agent 动手时所在的会话。它在自己这个 issue 会话里的动作
不投回那个会话（避免自激）；issue 只在它自己的会话里做，普通对话只建、派 issue。

用户的评论在按上述规则没有 agent 接手时投给负责项目的 agent（owner），见 rules.recipient。

依赖（graph）：还在等上游的 issue 不投、不算停滞（rules.recipient / rules.stalled 看 Snapshot.waiting_on）。上游进入
done / cancelled 时，在同一事务里给每个下游记一条 DEPENDENCY 动态并按同一条规则投递：下游因此不再等待，那封信
就投出去；还在等别的上游就只记时间线。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import agent_names_in, runnable_agent_in
from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.issues import conversation
from muliao.issues.delivery import IssueDelivery
from muliao.issues.events import PostIssueEvent
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
from muliao.issues.render import activity_views, issue_view, render_activity, render_issue
from muliao.issues.rules import (
    SYSTEM_ACTOR,
    USER,
    agent_assignee,
    ask_to_settle,
    cannot_review,
    cannot_start,
    cannot_stop_into,
    finished,
    finishing,
    handed_by,
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
from muliao.platform.tasks import Commit, no_commit
from muliao.platform.values import KEEP, Keep
from muliao.sessions.api import (
    Halted,
    Origin,
    SessionActivity,
    StopSession,
    WorkState,
    session_in,
)
from muliao.sessions.events import RunEnded, RunFailed, SessionDeleted

log = logging.getLogger(__name__)

TITLE_MAX = 300


def _session_events_lane(event: RunFailed | SessionDeleted) -> str:
    """同一个会话的事件在同一条 lane 上按发布顺序处理：RunFailed 一定先于 SessionDeleted 发布（会话忙时不能删除），
    它记下的动态（带这个会话）一定在删除的置空之前。RunEnded 不在这里：它在提交事务里确认会话还是那个 issue 会话才记。"""
    return f"issues:{event.session_id}"


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
    # 已结束（rules.finished）、用户能审核（rules.cannot_review 为 None）、用户能一键开工（rules.cannot_start 为 None）
    finished: bool
    reviewable: bool
    startable: bool


@dataclass(frozen=True)
class UserActions:
    """用户在一段时间里对一个 issue 做的事（评论与属性变化），每条渲染成与 issue_show 同样的一行（评论全文）。"""

    issue_id: int
    title: str
    # 此刻的状态
    status: IssueStatus
    lines: list[str]


def _title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidInput("issue title must not be empty")
    if len(title) > TITLE_MAX:
        raise InvalidInput(f"issue title is longer than {TITLE_MAX} characters")
    return title


def _refuse(issue_id: int, reason: str | None) -> None:
    """rules.cannot_* 给出了拒绝原因时报 Conflict（用户看到的是过时的状态：连点、两台设备、通知过时）。"""
    if reason is not None:
        raise Conflict(f"issue #{issue_id} {reason}")


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


@dataclass(frozen=True)
class Stopping:
    """IssueService.stop 的结果。"""

    issue: Issue
    # 被指派的 agent（停的是它的会话）
    agent: str
    # 真的停了一个会话；它还没有这个 issue 的会话时为 False
    stopped: bool


@contextmanager
def batch_item(size: int, n: int, key: str | None) -> Iterator[None]:
    """一批（size 项）里第 n 项的错误带上它是哪一项；只建一个时原样抛出。工具层解析 agent 名时也用它。"""
    try:
        yield
    except (InvalidInput, NotFound) as e:
        if size == 1:
            raise
        raise type(e)(f"{batch_label(n, key)}: {e}") from None


async def issue_exists_in(s: AsyncSession, issue_id: int) -> bool:
    """issue 存在（别的领域要把事件投到它之前，在自己的事务里只读地确认，不在时自己决定怎么反应；之后再被删除的由
    PostIssueEvent 的处理丢弃）。issue 只会从存在变为删除。"""
    return await s.get(Issue, issue_id) is not None


class IssueService:
    def __init__(
        self,
        db: Database,
        messaging: Messaging,
        tz: ZoneInfo,
        activity: SessionActivity,
        stop_session: StopSession,
    ):
        self._activity = activity
        self._stop_session = stop_session
        self._db = db
        self._tz = tz
        self._delivery = IssueDelivery(messaging, tz)

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentDeleting, "issues.agent_deleting", self.on_agent_deleting)
        messaging.subscribe(RunFailed, "issues.run_failed", self.on_run_failed, lane=_session_events_lane)
        messaging.subscribe(RunEnded, "issues.run_ended", self.on_run_ended)
        messaging.handle(
            PostIssueEvent,
            "issues.post_event",
            self.on_post_event,
            lane=lambda c: f"issue-event:{c.issue_id}",
        )
        messaging.subscribe(
            SessionDeleted, "issues.session_deleted", self.on_session_deleted, lane=_session_events_lane
        )

    async def on_session_deleted(self, event: SessionDeleted, _delivery: Delivery) -> Commit:
        """时间线上来自被删会话的动态保留，只是不再指向它（session_id 置空）。"""

        async def commit(s: AsyncSession) -> None:
            await s.execute(
                update(IssueActivity)
                .where(IssueActivity.session_id == event.session_id)
                .values(session_id=None)
            )

        return commit

    async def on_run_ended(self, event: RunEnded, _delivery: Delivery) -> Commit:
        """issue 会话里的一次运行结束了：issue 指派给这个 agent、却因此停滞（还是 in_progress，会话空闲）时，以它的名义记一条
        UNSETTLED 并投进那个会话，请它表态（rules.ask_to_settle）。停在挂起的调用上、被用户停止的不问；会话已经又有事要做
        （信箱里还有信、在等后台任务）时不算停滞，不问。其他来源的会话、已被删除的 issue 不处理。"""
        if event.origin != Origin.ISSUE or event.stopped or event.paused:
            return no_commit
        issue_id = conversation.issue_of(event.session_id, event.origin_ref)

        async def commit(s: AsyncSession) -> None:
            issue = await s.get(Issue, issue_id)
            if issue is None:
                return
            current = await conversation.session_in(s, issue_id, event.agent_id)
            if current is None or current.id != event.session_id:
                # 事件迟到（人工重试）时会话已被删除或换了新的：那次运行已不是它在这个 issue 上的会话，不问
                return
            key = (issue_id, event.agent_id)
            state = (await conversation.work_states_in(self._activity, s, [key]))[key]
            snap = snapshot(issue, (await links_in(s, [issue_id]))[issue_id])
            if not ask_to_settle(snap, event.agent_id, state):
                return
            activity = self._record(s, issue, event.agent_id, event.session_id, ActivityKind.UNSETTLED)
            await self._post_in(s, issue, event.agent_id, None, [activity])
            log.info("issue left unsettled, asked", extra={"issue_id": issue_id, "run_id": event.run_id})

        return commit

    async def on_post_event(self, command: PostIssueEvent, _delivery: Delivery) -> Commit:
        """投到 issue 的一个事件：记一条 EVENT（actor 是事件来源所属的 agent，change 的 to 是来源的描述），按 rules.recipient
        投递：有被指派的 agent 接手时给它，否则像用户评论一样给 owner。不在任何 issue 会话里发生（session_id 为空），所以来源的
        agent 自己是指派人时也投给它。发送方校验之后 issue 被删除了就丢弃。"""

        async def commit(s: AsyncSession) -> None:
            issue = await s.get(Issue, command.issue_id)
            if issue is None:
                log.info("event to a deleted issue dropped", extra={"issue_id": command.issue_id})
                return
            activity = self._record(
                s,
                issue,
                command.agent_id,
                None,
                ActivityKind.EVENT,
                body=command.body,
                change=(None, command.source),
            )
            await self._post_in(s, issue, command.agent_id, None, [activity])
            log.info("issue event posted", extra={"issue_id": command.issue_id, "source": command.source})

        return commit

    async def on_run_failed(self, event: RunFailed, _delivery: Delivery) -> Commit:
        """issue 会话里的运行最终失败：在 issue 时间线上以该 agent 的名义记一笔（失败的是它的运行）。通知用户由通知领域
        订阅 RunFailed 做，这里不重复发。不投递这条动态、不改状态：投进会话就是自动重试，永久性的错误会陪它无限循环；
        之后有新动态时照常投递、唤醒它。其他来源的会话、已被删除的 issue 不处理。"""
        if event.origin != Origin.ISSUE:
            return no_commit
        issue_id = conversation.issue_of(event.session_id, event.origin_ref)

        async def commit(s: AsyncSession) -> None:
            issue = await s.get(Issue, issue_id)
            if issue is None:
                log.info(
                    "run failed on a deleted issue", extra={"issue_id": issue_id, "run_id": event.run_id}
                )
                return
            # 正常情况下同一会话的 SessionDeleted 排在后面（_session_events_lane）；投递失败后被人工重试时可能已在删除之后，
            # 那时不再指向它（之后不会再有它的 SessionDeleted 来置空）。会话只会从存在变为删除，本事务里检查没有竞态。
            session = event.session_id if await session_in(s, event.session_id) is not None else None
            self._record(s, issue, event.agent_id, session, ActivityKind.RUN_FAILED, body=event.summary)
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
                    self._record(
                        s, issue, SYSTEM_ACTOR, None, ActivityKind.ASSIGNEE, change=(event.agent_id, None)
                    )
                if issue.owner_agent_id == event.agent_id:
                    issue.owner_agent_id = None
                    self._record(
                        s, issue, SYSTEM_ACTOR, None, ActivityKind.OWNER, change=(event.agent_id, None)
                    )

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
            keyed = await conversation.sessions_among(s, [i.id for i in issues])
            keys = {i.id: (i.id, agent) for i in issues if (agent := agent_assignee(i.assignee)) is not None}
            states = await conversation.work_states_in(self._activity, s, list(keys.values()))
            links = await links_in(s, [i.id for i in issues])
        work = {}
        for i in issues:
            state = states[keys[i.id]] if i.id in keys else None
            snap = snapshot(i, links[i.id])
            work[i.id] = IssueWork(
                sessions={agent: info.id for agent, info in keyed[i.id].items()},
                assignee_state=state,
                stalled=stalled(snap, state),
                depends_on=links[i.id].depends_on,
                blocks=links[i.id].blocks,
                waiting_on=snap.waiting_on,
                finished=finished(i.status),
                reviewable=cannot_review(i.status, i.assignee) is None,
                startable=cannot_start(i.status, i.owner_agent_id) is None,
            )
        return work

    async def user_actions_between(
        self, start: datetime, end: datetime, agent_ids: Collection[str] | None
    ) -> list[UserActions]:
        """用户在 [start, end) 里对这些 agent 负责（owner）或被指派的 issue（None 为全部）做的事，按 issue 分组、
        按时间排序（热历史：history 领域的 activity_overview）。按 issue 此刻的 owner / 指派人筛：当天被转给别人的 issue 算新的
        负责人的（要看全部就不筛）。"""
        query = (
            select(IssueActivity, Issue)
            .join(Issue, Issue.id == IssueActivity.issue_id)
            .where(
                IssueActivity.actor == USER, IssueActivity.created_at >= start, IssueActivity.created_at < end
            )
            .order_by(IssueActivity.id)
        )
        if agent_ids is not None:
            query = query.where(
                or_(Issue.owner_agent_id.in_(list(agent_ids)), Issue.assignee.in_(list(agent_ids)))
            )
        async with self._db.transaction() as s:
            rows = (await s.execute(query)).all()
            names = await agent_names_in(s)
        grouped: dict[int, tuple[Issue, list[IssueActivity]]] = {}
        for activity, issue in rows:
            grouped.setdefault(issue.id, (issue, []))[1].append(activity)
        return [
            UserActions(
                issue_id=issue.id,
                title=issue.title,
                status=issue.status,
                lines=[render_activity(v) for v in activity_views(activities, names, self._tz)],
            )
            for issue, activities in grouped.values()
        ]

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
        description: str = NewIssue.description,
        status: IssueStatus = NewIssue.status,
        priority: IssuePriority = NewIssue.priority,
        assignee: str | None = NewIssue.assignee,
        depends_on: Sequence[int] = (),
    ) -> Issue:
        """建一个 issue（缺省值以 NewIssue 为准）。depends_on：它依赖的上游 issue；不能成环。"""
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
            for issue in created:
                happened = [self._record(s, issue, actor, session_id, ActivityKind.CREATED)]
                await self._post_in(s, issue, actor, session_id, happened)
        for issue in created:
            log.info(
                "issue created", extra={"issue_id": issue.id, "actor": actor, "assignee": issue.assignee}
            )
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
            happened = await self._apply_in(
                s,
                issue,
                actor,
                session_id,
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
            await self._post_in(s, issue, actor, session_id, happened)
        log.info("issue updated", extra={"issue_id": issue_id, "actor": actor, "status": issue.status})
        return issue

    async def approve(self, issue_id: int) -> Issue:
        """用户审核通过交给自己的 issue：设为 done（有下游时照常放行）。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            _refuse(issue_id, cannot_review(issue.status, issue.assignee))
            happened = await self._apply_in(s, issue, USER, None, status=IssueStatus.DONE)
            await self._post_in(s, issue, USER, None, happened)
        log.info("issue approved", extra={"issue_id": issue_id})
        return issue

    async def start(self, issue_id: int) -> Issue:
        """用户开工一个 backlog issue：指派给负责项目的 agent（owner）并进入 in_progress，按规则投递。已不在 backlog
        （连点、两台设备）或没有 owner 时拒绝。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            _refuse(issue_id, cannot_start(issue.status, issue.owner_agent_id))
            happened = await self._apply_in(
                s, issue, USER, None, status=IssueStatus.IN_PROGRESS, assignee=issue.owner_agent_id
            )
            await self._post_in(s, issue, USER, None, happened)
        log.info("issue started", extra={"issue_id": issue_id, "assignee": issue.assignee})
        return issue

    async def reject(self, issue_id: int, reason: str) -> Issue:
        """用户打回交给自己的 issue：理由记成评论，回到 in_progress，指派给把它交给用户的那个 agent（rules.handed_by）。
        三条动态一起投进那个 agent 的 issue 会话（一封信）。"""
        if not reason.strip():
            raise InvalidInput("the reason for sending an issue back must not be empty")
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            _refuse(issue_id, cannot_review(issue.status, issue.assignee))
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
            happened = [self._record(s, issue, USER, None, ActivityKind.COMMENT, body=reason)]
            happened += await self._apply_in(
                s, issue, USER, None, status=IssueStatus.IN_PROGRESS, assignee=agent
            )
            await self._post_in(s, issue, USER, None, happened)
        log.info("issue sent back", extra={"issue_id": issue_id, "assignee": agent})
        return issue

    async def stop(
        self,
        actor: str,
        issue_id: int,
        *,
        session_id: str | None,
        status: IssueStatus,
        message: str | None,
    ) -> Stopping:
        """叫停被指派 agent 在这个 issue 上的会话（issue_stop，#272）：停掉它（同用户点停止，连同它开出去的 thread），把 issue
        改成 status，时间线记 STOPPED 与状态变更。有 message 时记成评论，连同这次的动态投进被停下的会话跑一轮（收尾或改方向），
        不论新状态是什么。

        规则见 rules.cannot_stop_into（停下不是做完；留在 in_progress 必须带 message）。
        没有 agent 指派人时报 Conflict（没有会话可停，改状态用 update）；在要停的那个会话里调用报 InvalidInput。指派人还没有
        这个会话（或它刚被删除）时没有可停的：只改状态、记评论，按 rules.recipient 投递，不为收尾新建会话。

        先停后改：停止返回时停止事务已提交（sessions.api.StopSession），之后投的信不会被它清掉。两个事务之间指派人
        被改了也不改目标：STOPPED 记的、收尾消息投的都是被停下的那个 agent。从被停会话开出去的 thread 里调用时，
        那个 thread 随停止被逐层停掉，可能在第二个事务之前就被取消：会话停了而状态没改，调用方看不到结果（契约之外的
        用法，不另拦）。"""
        if message is not None and not message.strip():
            raise InvalidInput("message must not be empty")
        if (reason := cannot_stop_into(status, message is not None)) is not None:
            raise InvalidInput(reason)
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            agent = agent_assignee(issue.assignee)
            if agent is None:
                raise Conflict(
                    f"issue #{issue_id} is not assigned to an agent: there is no conversation to stop "
                    "(use issue_update to change it)"
                )
            target = await conversation.session_in(s, issue_id, agent)
        if target is not None and target.id == session_id:
            raise InvalidInput(
                f"this is the conversation of issue #{issue_id} itself: change the issue with issue_update instead"
            )
        stopped = target is not None and await self._stop_session(target.id) == Halted.STOPPED
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            happened = []
            if stopped:
                happened.append(
                    self._record(s, issue, actor, session_id, ActivityKind.STOPPED, change=(None, agent))
                )
            happened += await self._apply_in(s, issue, actor, session_id, status=status)
            if message is not None:
                happened.append(self._record(s, issue, actor, session_id, ActivityKind.COMMENT, body=message))
            if happened:
                await self._post_in(
                    s,
                    issue,
                    actor,
                    session_id,
                    happened,
                    to=agent if stopped and message is not None else None,
                )
        log.info(
            "issue conversation stopped",
            extra={
                "issue_id": issue_id,
                "actor": actor,
                "agent": agent,
                "stopped": stopped,
                "status": status,
            },
        )
        return Stopping(issue=issue, agent=agent, stopped=stopped)

    async def _apply_in(
        self,
        s: AsyncSession,
        issue: Issue,
        actor: str,
        session_id: str | None,
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
            happened.append(self._record(s, issue, actor, session_id, kind, change=change))

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

    async def _post_in(
        self,
        s: AsyncSession,
        issue: Issue,
        actor: str,
        session_id: str | None,
        happened: Sequence[IssueActivity],
        *,
        to: str | None = None,
    ) -> None:
        """记下的这次动态（非空）带出的一切，只从动态本身推出：通知与投递（IssueDelivery.post_in，to 同它），
        进入 done / cancelled 时放行下游（rules.finishing）。记了动态却没有交给它的，只有不投递的 on_run_failed 与
        系统清理 on_agent_deleting。"""
        await self._delivery.post_in(s, issue, actor, session_id, happened, to=to)
        if (was := finishing(happened)) is not None:
            await self._unblock_in(s, issue, was, actor, session_id)

    async def comment(
        self, actor: str, issue_id: int, body: str, *, session_id: str | None = None
    ) -> IssueActivity:
        if not body.strip():
            raise InvalidInput("comment must not be empty")
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            activity = self._record(s, issue, actor, session_id, ActivityKind.COMMENT, body=body)
            await self._post_in(s, issue, actor, session_id, [activity])
        log.info("issue commented", extra={"issue_id": issue_id, "actor": actor})
        return activity

    async def delete(self, issue_id: int) -> None:
        """删除 issue（用户的操作）。依赖它的下游少了这个上游：各记一条 DEPENDS_ON 动态，按规则投递（可能因此不再等待）。"""
        async with self._db.transaction() as s:
            issue = await self._get_in(s, issue_id)
            downstream = await downstream_in(s, issue_id)
            before = await links_in(s, downstream)
            await s.delete(issue)
            await s.flush()
            for down_id in downstream:
                down = await self._get_in(s, down_id)
                old = ids_of(before[down_id].depends_on)
                change = (old, [i for i in old if i != issue_id])
                happened = [self._record(s, down, USER, None, ActivityKind.DEPENDS_ON, change=change)]
                await self._post_in(s, down, USER, None, happened)
        log.info("issue deleted", extra={"issue_id": issue_id, "downstream": downstream})

    # ---- 内部 ----

    async def _get_in(self, s: AsyncSession, issue_id: int) -> Issue:
        issue = await s.get(Issue, issue_id)
        if issue is None:
            raise NotFound(f"issue #{issue_id} not found")
        return issue

    async def _unblock_in(
        self, s: AsyncSession, issue: Issue, was: IssueStatus, actor: str, session_id: str | None
    ) -> None:
        """issue 进入 done / cancelled：每个下游记一条 DEPENDENCY 动态，按 rules.recipient 投递——不再等别的上游时，
        这就是让它开工的那封信。"""
        downstream_ids = await downstream_in(s, issue.id)
        for down in downstream_ids:
            downstream = await self._get_in(s, down)
            happened = [
                self._record(
                    s,
                    downstream,
                    actor,
                    session_id,
                    ActivityKind.DEPENDENCY,
                    change=(was, issue.status),
                    upstream=issue.id,
                )
            ]
            await self._post_in(s, downstream, actor, session_id, happened)
        if downstream_ids:
            log.info("issue unblocked downstream", extra={"issue_id": issue.id, "status": issue.status})

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
        session_id: str | None,
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
            session_id=session_id,
        )
        s.add(activity)
        # 时间线上多一条就是 issue 变了：updated_at 只在这里更新
        issue.updated_at = activity.created_at
        return activity
