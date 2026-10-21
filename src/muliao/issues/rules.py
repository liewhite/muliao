"""issue 的纯规则：什么时候唤醒被指派的 agent、交给它的信件长什么样。"""

from collections.abc import Sequence
from dataclasses import dataclass

from muliao.issues.tables import ActivityKind, IssueStatus

# 指派人的取值：agent id，或这个常量表示用户；None 为未指派。agent id 是 12 位十六进制，不会与它相同
USER = "user"

# 「该有人动手」的状态：处于这两个状态、指派给 agent 的 issue 会唤醒它
ACTIVE = frozenset({IssueStatus.TODO, IssueStatus.IN_PROGRESS})


@dataclass(frozen=True)
class Snapshot:
    """唤醒判定看的那部分 issue 状态。"""

    status: IssueStatus
    assignee: str | None


def wake_target(before: Snapshot | None, after: Snapshot, working: str | None) -> str | None:
    """这次变化要唤醒的 agent（没有为 None）。before 为 None 表示新建。

    working 是这次修改不该唤醒的 agent：修改是它自己做的（见 IssueService._working），否则为 None。

    被指派人是 agent、且不是 working 时：
    - 状态从别的状态转入 todo / in_progress；
    - 或处于 todo / in_progress 时被指派给它（含新建时就指派好）。
    """
    agent = after.assignee
    if agent is None or agent == USER or agent == working or after.status not in ACTIVE:
        return None
    if before is None or before.status not in ACTIVE or before.assignee != agent:
        return agent
    return None


def comment_wakes(issue: Snapshot, working: str | None) -> str | None:
    """评论唤醒被指派的 agent：issue 处于 todo / in_progress、指派给 agent、不是它自己评论的。"""
    agent = issue.assignee
    if agent is None or agent == USER or agent == working or issue.status not in ACTIVE:
        return None
    return agent


@dataclass(frozen=True)
class ActivityView:
    """渲染给模型看的一条时间线。actor 是展示名（「用户」或 agent 名）。"""

    at: str
    actor: str
    kind: ActivityKind
    body: str | None
    before: str | None
    after: str | None


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
    activities: Sequence[ActivityView]


def render_issue(issue: IssueView) -> str:
    """issue 的完整文本（交给模型：唤醒信件与 issue_show 共用）。"""
    lines = [
        f"# Issue #{issue.id}: {issue.title}",
        f"owner (project): {issue.owner} | status: {issue.status} | priority: {issue.priority} | "
        f"assignee: {issue.assignee} | created by: {issue.creator}",
        "",
        issue.description.strip() or "(no description)",
        "",
        "## Activity",
    ]
    for a in issue.activities:
        if a.kind == ActivityKind.COMMENT:
            lines.append(f"- [{a.at}] {a.actor} commented:\n  " + (a.body or "").replace("\n", "\n  "))
        elif a.kind == ActivityKind.CREATED:
            lines.append(f"- [{a.at}] {a.actor} created the issue")
        elif a.kind == ActivityKind.CLAIMED:
            lines.append(f"- [{a.at}] {a.actor} claimed the issue for the conversation working on it")
        elif a.kind == ActivityKind.RELEASED:
            lines.append(f"- [{a.at}] {a.actor} released the issue from the conversation working on it")
        elif a.kind == ActivityKind.DESCRIPTION:
            lines.append(f"- [{a.at}] {a.actor} edited the description")
        else:
            lines.append(f"- [{a.at}] {a.actor} changed {a.kind}: {a.before} -> {a.after}")
    return "\n".join(lines)


WAKE_GUIDE = """\
You are the assignee of this issue on the muliao issue board. Work on it, then record the outcome on the issue \
with the `issue_*` tools: comment with what you did or found (`issue_comment`), and move it on with \
`issue_update` — e.g. status `in_progress` while working, `blocked` with a comment explaining why. When you \
have finished, do not set `done` yourself: set status `in_review` and assignee `user`, and the user moves it to \
`done` after reviewing. Hand it to another agent by changing the assignee. The user reads the issue, not this \
conversation."""


def wake_message(reason: str, rendered: str) -> str:
    return f"{reason}\n\n{WAKE_GUIDE}\n\n<issue>\n{rendered}\n</issue>"
