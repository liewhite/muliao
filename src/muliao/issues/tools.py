"""issue 看板的工具：agent 按名称指代被指派人（`user` 指用户），不出现 agent id。"""

from datetime import datetime

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.api import AgentNames, AgentService
from muliao.errors import InvalidInput
from muliao.issues.rules import USER
from muliao.issues.service import IssueService, IssueWork
from muliao.issues.tables import Issue, IssuePriority, IssueStatus
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.platform.values import KEEP, Keep
from muliao.sessions.api import WorkState


class IssueSummary(BaseModel):
    id: int
    title: str
    # 负责的项目（agent 名）；负责的 agent 被删除后为 null
    owner: str | None
    status: IssueStatus
    priority: IssuePriority
    # agent 名、`user`，或 null（未指派）
    assignee: str | None
    # 被指派 agent 在这个 issue 会话里的状态：running（在跑）、waiting（等后台任务、委派或用户回答）、idle（空闲）；
    # 没指派给 agent 或它还没有这个 issue 的会话时为 null
    assignee_session: WorkState | None
    # todo / in_progress、指派给 agent，它的 issue 会话却空闲或不存在：该有人做却没人在做
    stalled: bool
    # 它依赖的上游 issue，与其中还没完成（不是 done / cancelled）的：waiting_on 非空时它还轮不到，动态不投、不算停滞
    depends_on: list[int]
    waiting_on: list[int]
    updated_at: datetime


def issues_toolset(issues: IssueService, agents: AgentService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    async def assignee_id(name: str) -> str:
        return USER if name == USER else (await agents.find_by_name(name)).id

    def summary(issue: Issue, names: AgentNames, work: IssueWork) -> IssueSummary:
        assignee = issue.assignee
        return IssueSummary(
            id=issue.id,
            title=issue.title,
            owner=None if issue.owner_agent_id is None else names[issue.owner_agent_id],
            status=issue.status,
            priority=issue.priority,
            assignee=None if assignee is None else USER if assignee == USER else names[assignee],
            assignee_session=work.assignee_session,
            stalled=work.stalled,
            depends_on=[d.id for d in work.depends_on],
            waiting_on=list(work.waiting_on),
            updated_at=issue.updated_at,
        )

    @ts.tool_plain
    async def issue_list(
        statuses: tuple[IssueStatus, ...] = (), assignee: str | None = None, owner: str | None = None
    ) -> list[IssueSummary]:
        """List issues on the muliao issue board, most recently updated first. `assignee_session` tells whether
        the assigned agent is working on it in the issue's conversation: running, waiting (on a background job,
        delegated work or the user's answer), idle, or null (no agent assignee or no conversation yet). `stalled`
        marks a `todo`/`in_progress` issue assigned to an agent whose issue conversation is idle or missing.
        `waiting_on` lists dependencies not yet `done`/`cancelled`; such an issue is not delivered or stalled.

        Args:
            statuses: Only these statuses; all when omitted.
            assignee: Only issues assigned to this agent (by name) or `user`.
            owner: Only issues owned by this agent (by name): the whole history of its project.
        """
        names = await agents.names()
        found = await issues.list(
            statuses=list(statuses),
            assignee=None if assignee is None else await assignee_id(assignee),
            owner=None if owner is None else (await agents.find_by_name(owner)).id,
        )
        work = await issues.work(found)
        return [summary(i, names, work[i.id]) for i in found]

    @ts.tool_plain
    async def issue_show(issue_id: int) -> str:
        """Show an issue with its description, dependencies, comments and history.

        Args:
            issue_id: Issue number.
        """
        return await issues.render(issue_id)

    @ts.tool
    async def issue_create(
        ctx: RunContext[RunScope],
        title: str,
        owner: str,
        description: str = "",
        status: IssueStatus = IssueStatus.BACKLOG,
        priority: IssuePriority = IssuePriority.NONE,
        assignee: str | None = None,
        depends_on: tuple[int, ...] = (),
    ) -> str:
        """Create an issue. Each agent works on an issue in one conversation dedicated to it; while the issue is
        `todo` or `in_progress`, its creation and every later change are delivered there to the assigned agent,
        except changes made from that very conversation (also when the assignee is you: create it here, it is
        worked on there). Use `backlog` for later work.
        To run issues in order, give `depends_on`: the issue is delivered only once all of them are `done` or
        `cancelled` (changes before that are only recorded), so a chain can be created as `todo` up front.

        Args:
            title: One-line summary.
            owner: The agent (by name) whose project this is; it stays the owner when the issue is reassigned.
                Usually yourself for your own project's work.
            description: Markdown details: context, what done looks like.
            status: Initial status.
            priority: Priority.
            assignee: Agent name, `user`, or omit for unassigned.
            depends_on: Issue numbers this one waits for; dependencies must not form a cycle.
        """
        issue = await issues.create(
            ctx.deps.agent_id,
            session_id=ctx.deps.session_id,
            owner=(await agents.find_by_name(owner)).id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee=None if assignee is None else await assignee_id(assignee),
            depends_on=depends_on,
        )
        return f"created issue #{issue.id}"

    @ts.tool
    async def issue_update(
        ctx: RunContext[RunScope],
        issue_id: int,
        owner: str | None = None,
        title: str | None = None,
        description: str | None = None,
        status: IssueStatus | None = None,
        priority: IssuePriority | None = None,
        assignee: str | None = None,
        unassign: bool = False,
        depends_on: tuple[int, ...] | None = None,
    ) -> str:
        """Change an issue; only the given fields change. When the issue is (or now becomes) `todo`/`in_progress`
        and assigned to an agent, the change is delivered to that agent in its conversation for the issue, except
        when the agent makes it from that very conversation. Finished work goes to `in_review` with assignee
        `user`, never straight to `done`: the user moves it to `done` after reviewing.

        Args:
            issue_id: Issue number.
            owner: New owner agent (by name): the project this issue belongs to.
            title: New title.
            description: New description (replaces the old one).
            status: New status; `done` is for the user, set `in_review` when you have finished.
            priority: New priority.
            assignee: Agent name, or `user` to hand it to the user (they get a notification).
            unassign: Remove the assignee.
            depends_on: The full new list of issue numbers this one waits for (replaces the old list; `[]` removes
                all); dependencies must not form a cycle.
        """
        if assignee is not None and unassign:
            raise InvalidInput("give either assignee or unassign, not both")
        target: str | None | Keep = (
            None if unassign else KEEP if assignee is None else await assignee_id(assignee)
        )
        issue = await issues.update(
            ctx.deps.agent_id,
            issue_id,
            session_id=ctx.deps.session_id,
            owner=None if owner is None else (await agents.find_by_name(owner)).id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee=target,
            depends_on=depends_on,
        )
        return f"issue #{issue.id} is now {issue.status}"

    @ts.tool
    async def issue_comment(ctx: RunContext[RunScope], issue_id: int, body: str) -> str:
        """Comment on an issue (Markdown): progress, findings, results, questions for the user. Delivered like any
        other change (see `issue_update`).

        Args:
            issue_id: Issue number.
            body: Comment text.
        """
        await issues.comment(ctx.deps.agent_id, issue_id, body, session_id=ctx.deps.session_id)
        return f"commented on issue #{issue_id}"

    return ts
