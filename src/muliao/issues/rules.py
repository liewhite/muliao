"""issue 的纯规则：动态投给谁、什么算停滞、依赖不成环、投出的信件长什么样。"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from muliao.agents.api import USER, AgentNames
from muliao.issues.tables import ActivityKind, IssueStatus
from muliao.sessions.api import WorkState

# 指派人的取值：agent id，或 USER（agents.api，全系统指代用户的那个值）；None 为未指派。agent id 是 12 位十六进制，
# 不会与它相同；agent 名称也不能取它（agents.naming.new_agent_name），工具里按名称指派时不会混淆


# 时间线上「系统」做的动作的 actor（例如删除 agent 时清理指派人、负责人）：既不是用户（None）也不是某个 agent。
# 不是 12 位十六进制，不会与 agent id 相同。名为 "system" 的 agent 在展示上会与它同名，只影响展示（没有按 actor
# 名称做的操作），所以不像 USER 那样保留这个名称
SYSTEM_ACTOR = "system"

# 运行失败记在时间线上、发给用户的错误摘要的上限（字符）：完整错误在任务页与日志里
FAILURE_SUMMARY_MAX = 500


def failure_summary(error: str) -> str:
    """运行失败的错误摘要：去掉首尾空白，超长时截断并标出。"""
    error = error.strip()
    return error if len(error) <= FAILURE_SUMMARY_MAX else error[: FAILURE_SUMMARY_MAX - 1] + "…"


def actor_name(actor: str | None, names: AgentNames) -> str:
    """时间线 actor 的展示名：用户（None）是 USER，系统是 SYSTEM_ACTOR，agent 按 id 换成名字。"""
    if actor is None:
        return USER
    return SYSTEM_ACTOR if actor == SYSTEM_ACTOR else names[actor]


def assignee_name(assignee: str, names: AgentNames) -> str:
    """指派人的展示名：用户就是 USER，agent 按 id 换成名字。"""
    return USER if assignee == USER else names[assignee]


# 「该有人动手」的状态：处于这两个状态、指派给 agent 的 issue，动态投给它
ACTIVE = frozenset({IssueStatus.TODO, IssueStatus.IN_PROGRESS})

# 上游处于这两个状态就不再挡下游：cancelled 也解除，由下游自己判断还做不做
FINISHED = frozenset({IssueStatus.DONE, IssueStatus.CANCELLED})


@dataclass(frozen=True)
class Snapshot:
    """投递与停滞判定看的那部分 issue 状态（动作之后的）。"""

    status: IssueStatus
    assignee: str | None
    # 负责项目的 agent id；负责的 agent 被删除后为 None
    owner: str | None
    # 还没完成（不在 FINISHED）的上游 issue；非空时这个 issue 在等上游，还轮不到它
    waiting_on: tuple[int, ...]


def reviewable(status: IssueStatus, assignee: str | None) -> bool:
    """用户能审核（通过或打回）的 issue：当前指派给用户、还没结束。"""
    return assignee == USER and status not in FINISHED


def handed_by(creator: str | None, assigners: Sequence[str | None]) -> str | None:
    """把当前指派给用户的 issue 交给用户的 agent（用户打回时指派回给它）：最近一条指派动态的 actor（它把指派人
    改成了用户）；从未改过指派人时是创建者（创建时就指派给了用户）。assigners 是按时间排序的各条指派动态的 actor。
    这次交接是用户自己或系统做的时为 None。"""
    actor = assigners[-1] if assigners else creator
    return None if actor in (None, SYSTEM_ACTOR) else actor


def _agent_assignee(issue: Snapshot) -> str | None:
    return None if issue.assignee in (None, USER) else issue.assignee


def _assignee_recipient(issue: Snapshot) -> str | None:
    """该由被指派的 agent 接手时就是它：todo / in_progress、指派给 agent、不在等上游。"""
    agent = _agent_assignee(issue)
    return None if agent is None or issue.status not in ACTIVE or issue.waiting_on else agent


def recipient(issue: Snapshot, *, user_comment: bool, from_its_session: str | None) -> str | None:
    """issue 上的一条动态（创建、评论、任何字段的修改）投给谁：动作之后处于 todo / in_progress（包括这次动作让它进入）
    时，投给被指派的 agent；backlog 是以后再做，in_review / done 等是已经交出去，不投。还在等上游（waiting_on 非空）
    时也不投：动态照常记进时间线，最后一个上游完成时投一封信，agent 自己 issue_show 看等待期间的动态。

    例外是用户的评论（user_comment）：按上面的规则没有 agent 接手时（backlog、blocked、in_review、done、cancelled，
    没指派或指派给用户，在等上游），投给负责项目的 agent，由它判断关掉、派人还是回复；有被指派的 agent 接手时只投给
    它，同一件事只有一个 agent 负责。agent 的评论与字段修改不唤起 owner，避免 agent 之间来回唤醒。

    from_its_session 是在自己这个 issue 会话里动手的 agent（否则为 None）：它自己的动作不投回它正在做事的会话，
    避免自激循环。它在别的会话（如普通对话）里给自己建、派、转 todo，照常投进 issue 会话——issue 只在那里做。
    """
    agent = _assignee_recipient(issue)
    if agent is not None:
        return None if agent == from_its_session else agent
    return issue.owner if user_comment else None


def stalled(issue: Snapshot, assignee_session: WorkState | None) -> bool:
    """该有人做却没人在做：todo / in_progress、不在等上游、指派给 agent，它在这个 issue 上的会话空闲或不存在。
    界面与巡检共用。"""
    return _assignee_recipient(issue) is not None and assignee_session in (None, WorkState.IDLE)


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
    """渲染给模型看的一条时间线。actor 是展示名（用户、系统或 agent 名，见 actor_name）。"""

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
    if a.kind == ActivityKind.RUN_FAILED:
        return f"- [{a.at}] {a.actor}'s run in its issue session failed: {a.body}"
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
    完整内容由 agent 自己 issue_show；怎么处理 issue 的说明在会话来源的固定说明里（sessions.origins
    的 ISSUE），不在信里重复。投递时上游都已完成（rules.recipient），所以摘要列出上游及其状态，不列等待。"""
    summary = [f"#{issue.id}: {issue.title}", f"status: {issue.status}", f"assignee: {issue.assignee}"]
    if issue.depends_on:
        summary.append(f"depends on: {_dependencies(issue.depends_on)}")
    happened = "\n".join(render_activity(a) for a in issue.activities)
    return (
        f"New activity on issue {' | '.join(summary)}\n{happened}\n\n"
        f"Full issue (description, dependencies, whole timeline): `issue_show({issue.id})`."
    )
