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


class IssueSummary(BaseModel):
    id: int
    title: str
    status: IssueStatus
    priority: IssuePriority
    # agent 名、`user`，或 null（未指派）
    assignee: str | None
    updated_at: datetime


def issues_toolset(issues: IssueService, agents: AgentService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    async def assignee_id(name: str) -> str:
        return USER if name == USER else (await agents.find_by_name(name)).id

    async def summary(issue: Issue, names: dict[str, str]) -> IssueSummary:
        assignee = USER if issue.assigned_to_user else issue.assignee_agent_id
        return IssueSummary(
            id=issue.id,
            title=issue.title,
            status=issue.status,
            priority=issue.priority,
            assignee=None if assignee is None else names.get(assignee, assignee),
            updated_at=issue.updated_at,
        )

    @ts.tool_plain
    async def issue_list(
        statuses: list[IssueStatus] | None = None, assignee: str | None = None
    ) -> list[IssueSummary]:
        """List issues on the muliao issue board, most recently updated first.

        Args:
            statuses: Only these statuses; all when omitted.
            assignee: Only issues assigned to this agent (by name) or `user`.
        """
        names = {a.id: a.name for a in await agents.list()} | {USER: USER}
        found = await issues.list(
            statuses=statuses, assignee=None if assignee is None else await assignee_id(assignee)
        )
        return [await summary(i, names) for i in found]

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
        """Create an issue. An agent assigned an issue in `todo` or `in_progress` is woken up to work on it.

        Args:
            title: One-line summary.
            description: Markdown details: context, what done looks like.
            status: Initial status.
            priority: Priority.
            assignee: Agent name, `user`, or omit for unassigned.
        """
        issue = await issues.create(
            ctx.deps.agent_id,
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
        assigning an issue in those statuses to an agent, wakes that agent up (never yourself).

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
        await issues.comment(ctx.deps.agent_id, issue_id, body)
        return f"commented on issue #{issue_id}"

    return ts
