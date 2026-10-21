"""issue 的纯规则：动态投给谁、什么算停滞、投出的信件长什么样。"""

from collections.abc import Sequence
from dataclasses import dataclass

from muliao.issues.tables import ActivityKind, IssueStatus
from muliao.sessions.api import WorkState

# 指派人的取值：agent id，或这个常量表示用户；None 为未指派。agent id 是 12 位十六进制，不会与它相同
USER = "user"

# 「该有人动手」的状态：处于这两个状态、指派给 agent 的 issue，动态投给它
ACTIVE = frozenset({IssueStatus.TODO, IssueStatus.IN_PROGRESS})


@dataclass(frozen=True)
class Snapshot:
    """投递与停滞判定看的那部分 issue 状态（动作之后的）。"""

    status: IssueStatus
    assignee: str | None


def _agent_assignee(issue: Snapshot) -> str | None:
    return None if issue.assignee in (None, USER) else issue.assignee


def recipient(issue: Snapshot, from_its_session: str | None) -> str | None:
    """issue 上的一条动态（创建、评论、任何字段的修改）投给谁：动作之后处于 todo / in_progress（包括这次动作让它进入）
    时，投给被指派的 agent；backlog 是以后再做，in_review / done 等是已经交出去，不投。

    from_its_session 是在自己这个 issue 会话里动手的 agent（否则为 None）：它自己的动作不投回它正在做事的会话，
    避免自激循环。它在别的会话（如普通对话）里给自己建、派、转 todo，照常投进 issue 会话——issue 只在那里做。
    """
    agent = _agent_assignee(issue)
    if agent is None or agent == from_its_session or issue.status not in ACTIVE:
        return None
    return agent


def stalled(issue: Snapshot, assignee_session: WorkState | None) -> bool:
    """该有人做却没人在做：todo / in_progress、指派给 agent，它在这个 issue 上的会话空闲或不存在。界面与巡检共用。"""
    return (
        _agent_assignee(issue) is not None
        and issue.status in ACTIVE
        and assignee_session in (None, WorkState.IDLE)
    )


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


def render_activity(a: ActivityView) -> str:
    if a.kind == ActivityKind.COMMENT:
        return f"- [{a.at}] {a.actor} commented:\n  " + (a.body or "").replace("\n", "\n  ")
    if a.kind == ActivityKind.CREATED:
        return f"- [{a.at}] {a.actor} created the issue"
    if a.kind == ActivityKind.DESCRIPTION:
        return f"- [{a.at}] {a.actor} edited the description"
    return f"- [{a.at}] {a.actor} changed {a.kind}: {a.before} -> {a.after}"


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
    lines.extend(render_activity(a) for a in issue.activities)
    return "\n".join(lines)


GUIDE = """\
You are the assignee of this issue on the muliao issue board, and this conversation is dedicated to it: every \
change others make to the issue (comments, status, assignee, edits) arrives here while it is `todo` or \
`in_progress`. Work on it here, then record the outcome on the issue with the `issue_*` tools: comment with what you \
did or found (`issue_comment`), and move it on with `issue_update` — e.g. status `in_progress` while working, \
`blocked` with a comment explaining why. When you have finished, do not set `done` yourself: set status \
`in_review` and assignee `user`, and the user moves it to `done` after reviewing. Hand it to another agent by \
changing the assignee. The user reads the issue, not this conversation."""


def delivery_message(issue_id: int, happened: Sequence[ActivityView], rendered: str) -> str:
    """投进 issue 会话的一封信：这次的动态，加上 issue 的当前视图。"""
    news = "\n".join(render_activity(a) for a in happened)
    return f"New activity on issue #{issue_id}:\n{news}\n\n{GUIDE}\n\n<issue>\n{rendered}\n</issue>"
