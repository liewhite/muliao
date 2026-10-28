"""issue 给模型看的文本：issue_show 的完整内容与投进 issue 会话的信件。只读视图，从 issue、时间线与依赖图组装。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from muliao.agents.api import AgentNames
from muliao.issues.graph import Dependency, Links, waiting_on
from muliao.issues.rules import actor_name, assignee_name
from muliao.issues.tables import ActivityKind, Issue, IssueActivity
from muliao.platform.values import local_time


@dataclass(frozen=True)
class ActivityView:
    """渲染给模型看的一条时间线。actor 是展示名（用户、系统或 agent 名，见 rules.actor_name）。"""

    at: str
    actor: str
    kind: ActivityKind
    body: str | None
    before: str | None
    after: str | None
    # DEPENDENCY 动态：完成的那个上游
    upstream: int | None = None


@dataclass(frozen=True)
class IssueView:
    id: int
    title: str
    description: str
    status: str
    priority: str
    assignee: str
    owner: str
    creator: str
    # 它依赖的上游、依赖它的下游
    depends_on: Sequence[Dependency]
    blocks: Sequence[Dependency]
    activities: Sequence[ActivityView]


def issue_view(
    issue: Issue, activities: Sequence[IssueActivity], links: Links, names: AgentNames, tz: ZoneInfo
) -> IssueView:
    """issue 连同给出的时间线（完整的，或只是这次的动态）换成展示名与本地时间。"""

    def assignee(value: Any) -> str:
        return "unassigned" if value is None else assignee_name(value, names)

    def issues(found: list[int]) -> str:
        return ", ".join(f"#{i}" for i in found) or "none"

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
                at=local_time(a.created_at, tz),
                actor=actor_name(a.actor, names),
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
        creator=actor_name(issue.creator, names),
        depends_on=links.depends_on,
        blocks=links.blocks,
        activities=views,
    )


def render_activity(a: ActivityView) -> str:
    if a.kind == ActivityKind.COMMENT:
        return f"- [{a.at}] {a.actor} commented:\n  " + (a.body or "").replace("\n", "\n  ")
    if a.kind == ActivityKind.CREATED:
        return f"- [{a.at}] {a.actor} created the issue"
    if a.kind == ActivityKind.DESCRIPTION:
        return f"- [{a.at}] {a.actor} edited the description"
    if a.kind == ActivityKind.DEPENDENCY:
        return f"- [{a.at}] {a.actor} changed status of dependency #{a.upstream}: {a.before} -> {a.after}"
    if a.kind == ActivityKind.RUN_FAILED:
        return f"- [{a.at}] {a.actor}'s run in its issue session failed: {a.body}"
    if a.kind == ActivityKind.UNSETTLED:
        # 怎么表态的指引只在 SESSION_NOTE（缓存前缀），这里只记事实
        return (
            f"- [{a.at}] {a.actor}'s run in its issue session ended with the issue still `in_progress` and nothing "
            "said about what comes next: settle it now as the note on this conversation says (a comment alone does "
            "not settle it; you are asked again every time you stop while it is `in_progress`)"
        )
    return f"- [{a.at}] {a.actor} changed {a.kind}: {a.before} -> {a.after}"


def _dependencies(found: Sequence[Dependency]) -> str:
    return ", ".join(f"#{d.id} ({d.status})" for d in found) or "none"


def render_issue(issue: IssueView) -> str:
    """issue 的完整文本（交给模型，issue_show 用）。"""
    lines = [
        f"# Issue #{issue.id}: {issue.title}",
        f"owner (project): {issue.owner} | status: {issue.status} | priority: {issue.priority} | "
        f"assignee: {issue.assignee} | created by: {issue.creator}",
    ]
    if issue.depends_on or issue.blocks:
        waiting = " ".join(f"#{i}" for i in waiting_on(issue.depends_on)) or "none"
        lines.append(
            f"depends on: {_dependencies(issue.depends_on)} | waiting on: {waiting} | "
            f"blocks: {_dependencies(issue.blocks)}"
        )
    lines += [
        "",
        issue.description.strip() or "(no description)",
        "",
        "## Activity",
    ]
    lines.extend(render_activity(a) for a in issue.activities)
    return "\n".join(lines)


def delivery_message(issue: IssueView) -> str:
    """投进 issue 会话的一封信：一行摘要加这次的动态（issue.activities 只含这次的动态），不随时间线变长。
    完整内容由 agent 自己 issue_show；怎么处理 issue 的说明在会话来源的固定说明里（rules.SESSION_NOTE），
    不在信里重复。投递时上游都已完成（rules.recipient），所以摘要列出上游及其状态，不列等待。"""
    summary = [f"#{issue.id}: {issue.title}", f"status: {issue.status}", f"assignee: {issue.assignee}"]
    if issue.depends_on:
        summary.append(f"depends on: {_dependencies(issue.depends_on)}")
    happened = "\n".join(render_activity(a) for a in issue.activities)
    return (
        f"New activity on issue {' | '.join(summary)}\n{happened}\n\n"
        f"Full issue (description, dependencies, whole timeline): `issue_show({issue.id})`."
    )
