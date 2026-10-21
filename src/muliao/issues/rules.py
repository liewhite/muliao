"""issue 的纯规则：动态投给谁、什么算停滞、依赖不成环、投出的信件长什么样。"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from muliao.issues.tables import ActivityKind, IssueStatus
from muliao.sessions.api import WorkState

# 指派人的取值：agent id，或这个常量表示用户；None 为未指派。agent id 是 12 位十六进制，不会与它相同
USER = "user"

# 「该有人动手」的状态：处于这两个状态、指派给 agent 的 issue，动态投给它
ACTIVE = frozenset({IssueStatus.TODO, IssueStatus.IN_PROGRESS})

# 上游处于这两个状态就不再挡下游：cancelled 也解除，由下游自己判断还做不做
FINISHED = frozenset({IssueStatus.DONE, IssueStatus.CANCELLED})


@dataclass(frozen=True)
class Snapshot:
    """投递与停滞判定看的那部分 issue 状态（动作之后的）。"""

    status: IssueStatus
    assignee: str | None
    # 还没完成（不在 FINISHED）的上游 issue；非空时这个 issue 在等上游，还轮不到它
    waiting_on: tuple[int, ...]


def _agent_assignee(issue: Snapshot) -> str | None:
    return None if issue.assignee in (None, USER) else issue.assignee


def recipient(issue: Snapshot, from_its_session: str | None) -> str | None:
    """issue 上的一条动态（创建、评论、任何字段的修改）投给谁：动作之后处于 todo / in_progress（包括这次动作让它进入）
    时，投给被指派的 agent；backlog 是以后再做，in_review / done 等是已经交出去，不投。还在等上游（waiting_on 非空）
    时也不投：动态照常记进时间线，最后一个上游完成时那封信带着完整的 issue 视图投过去。

    from_its_session 是在自己这个 issue 会话里动手的 agent（否则为 None）：它自己的动作不投回它正在做事的会话，
    避免自激循环。它在别的会话（如普通对话）里给自己建、派、转 todo，照常投进 issue 会话——issue 只在那里做。
    """
    agent = _agent_assignee(issue)
    if agent is None or agent == from_its_session or issue.status not in ACTIVE or issue.waiting_on:
        return None
    return agent


def stalled(issue: Snapshot, assignee_session: WorkState | None) -> bool:
    """该有人做却没人在做：todo / in_progress、不在等上游、指派给 agent，它在这个 issue 上的会话空闲或不存在。
    界面与巡检共用。"""
    return (
        _agent_assignee(issue) is not None
        and issue.status in ACTIVE
        and not issue.waiting_on
        and assignee_session in (None, WorkState.IDLE)
    )


def cycle_through(
    issue_id: int, depends_on: Iterable[int], edges: Iterable[tuple[int, int]]
) -> list[int] | None:
    """让 issue_id 依赖 depends_on 会不会成环：会则返回环上的路径（issue_id 依赖 ... 依赖 issue_id），否则 None。

    edges 是其余的依赖边（issue, 它依赖的上游），不含 issue_id 自己现有的上游（它们正被整体替换）。
    """
    upstream: dict[int, list[int]] = {}
    for down, up in edges:
        if down != issue_id:
            upstream.setdefault(down, []).append(up)
    seen: set[int] = set()
    stack = [(d, [issue_id, d]) for d in depends_on]
    while stack:
        node, path = stack.pop()
        if node == issue_id:
            return path
        if node in seen:
            continue
        seen.add(node)
        stack.extend((u, [*path, u]) for u in upstream.get(node, ()))
    return None


@dataclass(frozen=True)
class ActivityView:
    """渲染给模型看的一条时间线。actor 是展示名（「用户」或 agent 名）。"""

    at: str
    actor: str
    kind: ActivityKind
    body: str | None
    before: str | None
    after: str | None
    # DEPENDENCY 动态：完成的那个上游
    upstream: int | None = None


@dataclass(frozen=True)
class Dependency:
    """依赖关系里的另一个 issue（上游或下游）与它现在的状态。"""

    id: int
    status: IssueStatus


def waiting_on(depends_on: Iterable[Dependency]) -> tuple[int, ...]:
    """还没完成的上游：它们都完成（FINISHED）之前，这个 issue 还轮不到。"""
    return tuple(d.id for d in depends_on if d.status not in FINISHED)


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


def render_activity(a: ActivityView) -> str:
    if a.kind == ActivityKind.COMMENT:
        return f"- [{a.at}] {a.actor} commented:\n  " + (a.body or "").replace("\n", "\n  ")
    if a.kind == ActivityKind.CREATED:
        return f"- [{a.at}] {a.actor} created the issue"
    if a.kind == ActivityKind.DESCRIPTION:
        return f"- [{a.at}] {a.actor} edited the description"
    if a.kind == ActivityKind.DEPENDENCY:
        return f"- [{a.at}] {a.actor} changed status of dependency #{a.upstream}: {a.before} -> {a.after}"
    return f"- [{a.at}] {a.actor} changed {a.kind}: {a.before} -> {a.after}"


def _dependencies(found: Sequence[Dependency]) -> str:
    return ", ".join(f"#{d.id} ({d.status})" for d in found) or "none"


def render_issue(issue: IssueView) -> str:
    """issue 的完整文本（交给模型：唤醒信件与 issue_show 共用）。"""
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


GUIDE = """\
You are the assignee of this issue on the muliao issue board, and this conversation is dedicated to it: every \
change others make to the issue (comments, status, assignee, edits) arrives here while it is `todo` or \
`in_progress`. Work on it here, then record the outcome on the issue with the `issue_*` tools: comment with what you \
did or found (`issue_comment`), and move it on with `issue_update` — e.g. status `in_progress` while working, \
`blocked` with a comment explaining why. When you have finished, do not set `done` yourself: set status \
`in_review` and assignee `user`, and the user moves it to `done` after reviewing. Hand it to another agent by \
changing the assignee. An issue that depends on others is delivered only once all of them are `done` or \
`cancelled`; a cancelled dependency may mean this issue is no longer needed. The user reads the issue, not this \
conversation."""


def delivery_message(issue_id: int, happened: Sequence[ActivityView], rendered: str) -> str:
    """投进 issue 会话的一封信：这次的动态，加上 issue 的当前视图。"""
    news = "\n".join(render_activity(a) for a in happened)
    return f"New activity on issue #{issue_id}:\n{news}\n\n{GUIDE}\n\n<issue>\n{rendered}\n</issue>"
