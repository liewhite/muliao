"""issue 看板的工具：agent 按名称指代被指派人（`user` 指用户），不出现 agent id。"""

from datetime import datetime

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.api import AgentService
from muliao.errors import InvalidInput
from muliao.issues.rules import USER
from muliao.issues.service import KEEP, IssueService, Keep
from muliao.issues.tables import Issue, IssuePriority, IssueStatus
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.sessions.api import WorkState


class IssueSummary(BaseModel):
    id: int
    title: str
    status: IssueStatus
    priority: IssuePriority
    # agent 名、`user`，或 null（未指派）
    assignee: str | None
    # 被指派 agent 在这个 issue 会话里的状态：running（在跑）、waiting（等后台任务、委派或用户回答）、idle（空闲）；
    # 没指派给 agent 或它还没有这个 issue 的会话时为 null
    assignee_session: WorkState | None
    updated_at: datetime


def issues_toolset(issues: IssueService, agents: AgentService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    async def assignee_id(name: str) -> str:
        return USER if name == USER else (await agents.find_by_name(name)).id

    async def summary(
        issue: Issue, names: dict[str, str], states: dict[int, WorkState | None]
    ) -> IssueSummary:
        assignee = USER if issue.assigned_to_user else issue.assignee_agent_id
        return IssueSummary(
            id=issue.id,
            title=issue.title,
            status=issue.status,
            priority=issue.priority,
            assignee=None if assignee is None else names.get(assignee, assignee),
            assignee_session=states[issue.id],
            updated_at=issue.updated_at,
        )

    @ts.tool_plain
    async def issue_list(
        statuses: tuple[IssueStatus, ...] = (), assignee: str | None = None
    ) -> list[IssueSummary]:
        """List issues on the muliao issue board, most recently updated first. `assignee_session` tells whether
        the assigned agent is working on it in the issue's conversation: running, waiting (on a background job,
        delegated work or the user's answer), idle, or null (no agent assignee or no conversation yet).

        Args:
            statuses: Only these statuses; all when omitted.
            assignee: Only issues assigned to this agent (by name) or `user`.
        """
        names = {a.id: a.name for a in await agents.list()} | {USER: USER}
        found = await issues.list(
            statuses=list(statuses), assignee=None if assignee is None else await assignee_id(assignee)
        )
        states = await issues.work_states(found)
        return [await summary(i, names, states) for i in found]

    @ts.tool_plain
    async def issue_show(issue_id: int) -> str:
        """Show an issue with its description, comments and history.

        Args:
            issue_id: Issue number.
        """
        return await issues.render(issue_id)

    @ts.tool
    async def issue_create(
        ctx: RunContext[RunScope],
        title: str,
        description: str = "",
        status: IssueStatus = IssueStatus.BACKLOG,
        priority: IssuePriority = IssuePriority.NONE,
        assignee: str | None = None,
    ) -> str:
        """Create an issue. An agent assigned an issue in `todo` or `in_progress` is woken up to work on it;
        if that is you, this conversation becomes the one working on it (use `backlog` for later work).

        Args:
            title: One-line summary.
            description: Markdown details: context, what done looks like.
            status: Initial status.
            priority: Priority.
            assignee: Agent name, `user`, or omit for unassigned.
        """
        issue = await issues.create(
            ctx.deps.agent_id,
            session_id=ctx.deps.session_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee=None if assignee is None else await assignee_id(assignee),
        )
        return f"created issue #{issue.id}"

    @ts.tool
    async def issue_update(
        ctx: RunContext[RunScope],
        issue_id: int,
        title: str | None = None,
        description: str | None = None,
        status: IssueStatus | None = None,
        priority: IssuePriority | None = None,
        assignee: str | None = None,
        unassign: bool = False,
    ) -> str:
        """Change an issue; only the given fields change. Moving an agent's issue into `todo`/`in_progress`, or
        assigning an issue in those statuses to an agent, wakes that agent up. If that agent is you, nothing is
        started: this conversation becomes the one working on it, so carry on with it here (or leave it in
        `backlog` if it is for later).

        Args:
            issue_id: Issue number.
            title: New title.
            description: New description (replaces the old one).
            status: New status.
            priority: New priority.
            assignee: Agent name, or `user` to hand it to the user (they get a notification).
            unassign: Remove the assignee.
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
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee=target,
        )
        return f"issue #{issue.id} is now {issue.status}"

    @ts.tool
    async def issue_comment(ctx: RunContext[RunScope], issue_id: int, body: str) -> str:
        """Comment on an issue (Markdown): progress, findings, results, questions for the user.

        Args:
            issue_id: Issue number.
            body: Comment text.
        """
        await issues.comment(ctx.deps.agent_id, issue_id, body, session_id=ctx.deps.session_id)
        return f"commented on issue #{issue_id}"

    @ts.tool
    async def issue_release(ctx: RunContext[RunScope], issue_id: int) -> str:
        """Stop working on an issue in this conversation, e.g. it turned out to be separate from what you are doing
        here. If it is still `todo`/`in_progress` and assigned to you, a fresh conversation dedicated to the issue
        takes it over right away; otherwise it is simply detached.

        Args:
            issue_id: Issue number.
        """
        if ctx.deps.session_id is None:
            raise InvalidInput("issue_release only works inside a conversation")
        if await issues.release(ctx.deps.agent_id, issue_id, ctx.deps.session_id):
            return f"released issue #{issue_id}; a new conversation for it has been started"
        return f"released issue #{issue_id}"

    return ts
