"""issue 看板的工具：agent 按名称指代被指派人（`user` 指用户），不出现 agent id。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.api import AgentNames, AgentService
from muliao.errors import InvalidInput
from muliao.issues.rules import USER, assignee_name
from muliao.issues.service import IssueService, IssueWork, NewIssue, batch_item
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


class NewIssueItem(BaseModel):
    # issue_create 一次建多个时的一项（类 docstring 会进 schema，所以用注释）；字段与 issue_create 的单个参数一致（含义、
    # 默认值以这里为准，说明只写在 issue_create 的 Args 里），另加 key
    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(
        default=None, description="Name for this item that other items list in their depends_on, e.g. `api`."
    )
    title: str
    owner: str
    description: str = ""
    status: IssueStatus = IssueStatus.BACKLOG
    priority: IssuePriority = IssuePriority.NONE
    assignee: str | None = None
    depends_on: tuple[int | str, ...] = Field(
        default=(),
        description="What this one waits for: keys of other items of this batch and/or existing issue numbers.",
    )


def issues_toolset(board: IssueService, agents: AgentService) -> FunctionToolset[RunScope]:
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
            assignee=None if assignee is None else assignee_name(assignee, names),
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
        """List issues on the muliao issue board. Returns a JSON array of issues, most recently updated first, each
        with `id`, `title`, `owner`, `status`, `priority`, `assignee`, `assignee_session`, `stalled`, `depends_on`,
        `waiting_on` and `updated_at` (ISO 8601). `assignee_session` tells whether
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
        found = await board.list(
            statuses=list(statuses),
            assignee=None if assignee is None else await assignee_id(assignee),
            owner=None if owner is None else (await agents.find_by_name(owner)).id,
        )
        work = await board.work(found)
        return [summary(i, names, work[i.id]) for i in found]

    @ts.tool_plain
    async def issue_show(issue_id: int) -> str:
        """Show an issue with its description, dependencies, comments and history.

        Args:
            issue_id: Issue number.
        """
        return await board.render(issue_id)

    async def new_issue(item: NewIssueItem, size: int, n: int) -> NewIssue:
        with batch_item(size, n, item.key):
            owner = (await agents.find_by_name(item.owner)).id
            assignee = None if item.assignee is None else await assignee_id(item.assignee)
        return NewIssue(
            owner=owner,
            title=item.title,
            description=item.description,
            status=item.status,
            priority=item.priority,
            assignee=assignee,
            depends_on=item.depends_on,
            key=item.key,
        )

    @ts.tool
    async def issue_create(
        ctx: RunContext[RunScope],
        title: str | None = None,
        owner: str | None = None,
        description: str = "",
        status: IssueStatus = IssueStatus.BACKLOG,
        priority: IssuePriority = IssuePriority.NONE,
        assignee: str | None = None,
        depends_on: tuple[int, ...] = (),
        issues: tuple[NewIssueItem, ...] = (),
    ) -> str:
        """Create an issue. Each agent works on an issue in one conversation dedicated to it; while the issue is
        `todo` or `in_progress`, its creation and every later change are delivered there to the assigned agent
        (also when the assignee is you: create it here, it is worked on there), except changes made from that
        very conversation. Use `backlog` for later work.
        To run issues in order, give `depends_on`: the issue is delivered only once all of them are `done` or
        `cancelled` (changes before that are only recorded), so a chain can be created as `todo` up front.
        To create several issues that depend on each other, pass `issues` instead (one call, not several
        parallel calls: those cannot refer to each other's numbers): give each item a `key` and list other items'
        keys in its `depends_on`. The whole batch is created in one go (nothing is created if any item is
        invalid) and delivered only after all of it exists with its dependencies.
        To break down a larger piece of work: in one `issues` call, create the steps as `todo` issues (ordered
        among themselves with `depends_on`) and a `todo` parent issue with the overall goal that depends on all
        of them.

        Args:
            title: One-line summary (omit when using `issues`).
            owner: The agent (by name) whose project this is; it stays the owner when the issue is reassigned,
                and gets the user's comments while no agent is working on the issue. Usually yourself for your own
                project's work (omit when using `issues`).
            description: Markdown details: context, what done looks like.
            status: Initial status.
            priority: Priority.
            assignee: Agent name, `user`, or omit for unassigned.
            depends_on: Issue numbers this one waits for; dependencies must not form a cycle.
            issues: Several issues to create at once, each with the fields above plus an optional `key`; in
                their `depends_on`, a string is the key of another item of this batch, a number an existing issue.
        """
        if issues:
            optional = {
                "description": description,
                "status": status,
                "priority": priority,
                "assignee": assignee,
                "depends_on": depends_on,
            }
            defaults = {k: NewIssueItem.model_fields[k].default for k in optional}
            if title is not None or owner is not None or optional != defaults:
                raise InvalidInput("pass either one issue (title/owner/...) or issues, not both")
            new = [await new_issue(item, len(issues), n) for n, item in enumerate(issues)]
        else:
            if title is None or owner is None:
                raise InvalidInput("title and owner are required (or pass issues)")
            item = NewIssueItem(
                title=title,
                owner=owner,
                description=description,
                status=status,
                priority=priority,
                assignee=assignee,
                depends_on=depends_on,
            )
            new = [await new_issue(item, 1, 0)]
        created = await board.create_many(ctx.deps.agent_id, new, session_id=ctx.deps.session_id)
        if not issues:
            return f"created issue #{created[0].id}"
        lines = [
            f"#{issue.id}" + ("" if item.key is None else f" (key {item.key})") + f": {issue.title}"
            for item, issue in zip(issues, created, strict=True)
        ]
        return "created issues:\n" + "\n".join(lines)

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
        `user`, and the user moves it to `done` after reviewing; but when other issues depend on it (`issue_show`
        lists them under `blocks`), set it `done` yourself when finished, so the issues waiting for it can start.

        Args:
            issue_id: Issue number.
            owner: New owner agent (by name): the project this issue belongs to.
            title: New title.
            description: New description (replaces the old one).
            status: New status; when you have finished, `in_review`, or `done` if other issues depend on it.
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
        issue = await board.update(
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
        await board.comment(ctx.deps.agent_id, issue_id, body, session_id=ctx.deps.session_id)
        return f"commented on issue #{issue_id}"

    return ts
