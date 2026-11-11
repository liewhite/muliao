"""issue 给模型看的文本：issue_show 的完整内容与投进 issue 会话的信件。只读视图，从 issue、时间线与依赖图组装。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import assert_never
from zoneinfo import ZoneInfo

from muliao.agents.api import AgentNames
from muliao.issues.activity import (
    Activity,
    AssigneeChanged,
    Commented,
    Created,
    DependencyFinished,
    DependsOnChanged,
    DescriptionEdited,
    EventPosted,
    OwnerChanged,
    PriorityChanged,
    RunFailure,
    StatusChanged,
    Stopped,
    TitleChanged,
    Unsettled,
)
from muliao.issues.graph import Dependency, Links, waiting_on
from muliao.issues.rules import actor_name, assignee_name
from muliao.issues.tables import Issue
from muliao.platform.values import local_time


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
    # 时间线，每条已渲染成一行（render_activity）
    activities: Sequence[str]


def _assignee(value: str | None, names: AgentNames) -> str:
    return "unassigned" if value is None else assignee_name(value, names)


def _issues(found: list[int]) -> str:
    return ", ".join(f"#{i}" for i in found) or "none"


def _indented(body: str) -> str:
    return "\n  " + body.replace("\n", "\n  ")


def render_activity(a: Activity, names: AgentNames, tz: ZoneInfo) -> str:
    """时间线上的一条给模型看的文本：本地时间、展示名（用户、系统或 agent 名，见 rules.actor_name）。"""
    head = f"- [{local_time(a.created_at, tz)}]"
    actor = actor_name(a.actor, names)
    match a.what:
        case Created():
            return f"{head} {actor} created the issue"
        case Commented(body=body):
            return f"{head} {actor} commented:" + _indented(body)
        case DescriptionEdited():
            return f"{head} {actor} edited the description"
        case (
            TitleChanged(kind=kind, change=c)
            | StatusChanged(kind=kind, change=c)
            | PriorityChanged(kind=kind, change=c)
        ):
            return f"{head} {actor} changed {kind}: {c.before} -> {c.to}"
        case AssigneeChanged(kind=kind, change=c) | OwnerChanged(kind=kind, change=c):
            return f"{head} {actor} changed {kind}: {_assignee(c.before, names)} -> {_assignee(c.to, names)}"
        case DependsOnChanged(kind=kind, change=c):
            return f"{head} {actor} changed {kind}: {_issues(c.before)} -> {_issues(c.to)}"
        case DependencyFinished(change=c):
            return f"{head} {actor} changed status of dependency #{c.issue}: {c.before} -> {c.to}"
        case RunFailure(body=body):
            return f"{head} {actor}'s run in its issue session failed: {body}"
        case EventPosted(body=body, change=c):
            return f"{head} event from {c.to} ({actor}):" + _indented(body)
        case Stopped(change=c):
            return f"{head} {actor} stopped {_assignee(c.to, names)}'s conversation on this issue"
        case Unsettled():
            # 怎么表态的指引在 SESSION_NOTE（缓存前缀）；这里只多一句「只写评论不算」作提醒
            return (
                f"{head} {actor}'s run in its issue session ended with the issue still `in_progress` and nothing "
                "pending to wake it up: settle it now as the note on this conversation says (a comment alone does "
                "not settle it; you are asked again every time you stop like this)"
            )
        case other:
            assert_never(other)


def issue_view(
    issue: Issue, activities: Sequence[Activity], links: Links, names: AgentNames, tz: ZoneInfo
) -> IssueView:
    """issue 连同给出的时间线（完整的，或只是这次的动态）换成展示名与本地时间。"""
    return IssueView(
        id=issue.id,
        title=issue.title,
        description=issue.description,
        status=issue.status,
        priority=issue.priority,
        assignee=_assignee(issue.assignee, names),
        owner="none" if issue.owner_agent_id is None else names[issue.owner_agent_id],
        creator=actor_name(issue.creator, names),
        depends_on=links.depends_on,
        blocks=links.blocks,
        activities=[render_activity(a, names, tz) for a in activities],
    )


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
    lines.extend(issue.activities)
    return "\n".join(lines)


def delivery_message(issue: IssueView) -> str:
    """投进 issue 会话的一封信：一行摘要加这次的动态（issue.activities 只含这次的动态），不随时间线变长。
    完整内容由 agent 自己 issue_show；怎么处理 issue 的说明在会话来源的固定说明里（rules.SESSION_NOTE），
    不在信里重复。投递时上游都已完成（rules.recipient），所以摘要列出上游及其状态，不列等待。"""
    summary = [f"#{issue.id}: {issue.title}", f"status: {issue.status}", f"assignee: {issue.assignee}"]
    if issue.depends_on:
        summary.append(f"depends on: {_dependencies(issue.depends_on)}")
    happened = "\n".join(issue.activities)
    return (
        f"New activity on issue {' | '.join(summary)}\n{happened}\n\n"
        f"Full issue (description, dependencies, whole timeline): `issue_show({issue.id})`."
    )
