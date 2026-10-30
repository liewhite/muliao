"""issue 的纯规则：动态投给谁、什么算停滞、谁能审核、展示名。依赖图在 graph，给模型看的文本在 render。"""

from collections.abc import Sequence
from dataclasses import dataclass

from muliao.agents.api import USER, AgentNames
from muliao.issues.tables import IssueStatus
from muliao.sessions.api import WorkState

# 指派人的取值：agent id，或 USER（定义在 agents/ids.py、经 agents.api 导出，全系统指代用户的那个值）；None 为未指派。agent id 是 12 位十六进制，
# 不会与它相同；agent 名称也不能取它（agents.naming.new_agent_name），工具里按名称指派时不会混淆


# 时间线上「系统」做的动作的 actor（例如删除 agent 时清理指派人、负责人）：既不是用户（USER）也不是某个 agent。
# 不是 12 位十六进制，不会与 agent id 相同。名为 "system" 的 agent 在展示上会与它同名，只影响展示（没有按 actor
# 名称做的操作），所以不像 USER 那样保留这个名称
SYSTEM_ACTOR = "system"


def agent_actor(actor: str) -> str | None:
    """做这件事的是 agent 时是它的 id；用户（USER）或系统（SYSTEM_ACTOR）做的为 None。"""
    return None if actor in (USER, SYSTEM_ACTOR) else actor


def actor_name(actor: str, names: AgentNames) -> str:
    """时间线 actor 的展示名：用户是 USER，系统是 SYSTEM_ACTOR，agent 按 id 换成名字。"""
    return actor if agent_actor(actor) is None else names[actor]


def assignee_name(assignee: str, names: AgentNames) -> str:
    """指派人的展示名：用户就是 USER，agent 按 id 换成名字。"""
    return USER if assignee == USER else names[assignee]


# issue 会话（Origin.ISSUE）在指令里的说明：怎么处理一个 issue（会话级、不变，进缓存前缀；信件里不重复）。组合根交给
# 会话领域（sessions.api.OriginNotes）
SESSION_NOTE = (
    "This conversation is dedicated to one issue on the muliao issue board that you work on or own, and is "
    "continued each time the issue needs you again: while it is `in_progress`, every change to it "
    "(comments, status, assignee, edits, a dependency finishing) except those you make here arrives here as a short message "
    "carrying only that new activity. Read the full issue (description, dependencies, whole timeline) with "
    "`issue_show` before you start, and again whenever the message is not enough. An issue that depends on "
    "others is delivered only once all of them are `done` or `cancelled`; a cancelled dependency may mean "
    "this issue is no longer needed. Work on it here, then record the outcome on the issue with the `issue_*` "
    "tools: comment with what you did or found (`issue_comment`), and move it on with `issue_update`. "
    "Never end your turn with the issue left `in_progress` and nothing said about what comes next: before you "
    "stop, either finish it; or keep this conversation waiting on something that will wake it up here: a "
    "background job (`run_shell` with `background: true`, e.g. a loop polling for what you wait for), work "
    "delegated with `report_back: true`, or `wake_me` to be woken at a time you choose; or set `blocked`, "
    "assignee `user`, with a comment on what only the user can do. A comment alone does not count: when you stop "
    "with the issue `in_progress` and nothing pending here, the platform asks you again. When you have finished, check "
    "`issue_show` again: if other issues depend on this one (listed under `blocks`), it is a step of a larger piece "
    "of work, so set it `done` yourself and the next steps start; otherwise do not set `done` yourself: set "
    "status `in_review` and assignee `user`, and the user moves it to `done` after reviewing. Hand it to "
    "another agent by changing the assignee. A user comment on an issue no agent is working on (not `in_progress`, "
    "not assigned to an agent, or still waiting on dependencies) arrives here when you are "
    "its owner, even if it is not assigned to you: decide whether to close it, assign it to someone, or reply. "
    "Nobody is watching this conversation: the user reads the issue, not this conversation."
)

# 上游处于这两个状态就不再挡下游：cancelled 也解除，由下游自己判断还做不做
FINISHED = frozenset({IssueStatus.DONE, IssueStatus.CANCELLED})

# issue_stop 叫停之后 issue 可以进入的状态：停下不是做完，不能交审（in_review）或完成（done）；in_progress 是打断后改方向
STOP_STATUSES = frozenset(
    {IssueStatus.BACKLOG, IssueStatus.BLOCKED, IssueStatus.CANCELLED, IssueStatus.IN_PROGRESS}
)


@dataclass(frozen=True)
class Snapshot:
    """投递与停滞判定看的那部分 issue 状态（动作之后的）。"""

    status: IssueStatus
    assignee: str | None
    # 负责项目的 agent id；负责的 agent 被删除后为 None
    owner: str | None
    # 还没完成（不在 FINISHED）的上游 issue；非空时这个 issue 在等上游，还轮不到它
    waiting_on: tuple[int, ...]


def finished(status: IssueStatus) -> bool:
    """已结束（done / cancelled）：不再挡着依赖它的 issue。"""
    return status in FINISHED


def reviewable(status: IssueStatus, assignee: str | None) -> bool:
    """用户能审核（通过或打回）的 issue：当前指派给用户、还没结束。"""
    return assignee == USER and not finished(status)


def startable(status: IssueStatus, owner: str | None) -> bool:
    """用户能一键开工（指派给 owner 并进入 in_progress）的 issue：还在 backlog、有负责项目的 agent。"""
    return status == IssueStatus.BACKLOG and owner is not None


def handed_by(creator: str, assigners: Sequence[str]) -> str | None:
    """把当前指派给用户的 issue 交给用户的 agent（用户打回时指派回给它）：最近一条指派动态的 actor（它把指派人
    改成了用户）；从未改过指派人时是创建者（创建时就指派给了用户）。assigners 是按时间排序的各条指派动态的 actor。
    这次交接是用户自己或系统做的时为 None。"""
    return agent_actor(assigners[-1] if assigners else creator)


def agent_assignee(assignee: str | None) -> str | None:
    """指派给 agent 时是它的 id；未指派或指派给用户为 None。"""
    return None if assignee in (None, USER) else assignee


def _assignee_recipient(issue: Snapshot) -> str | None:
    """该由被指派的 agent 接手时就是它：in_progress、指派给 agent、不在等上游。"""
    agent = agent_assignee(issue.assignee)
    return None if agent is None or issue.status != IssueStatus.IN_PROGRESS or issue.waiting_on else agent


def recipient(issue: Snapshot, *, user_comment: bool, from_its_session: str | None) -> str | None:
    """issue 上的一条动态（创建、评论、任何字段的修改）投给谁：动作之后处于 in_progress（包括这次动作让它进入）
    时，投给被指派的 agent；backlog 是以后再做，in_review / done 等是已经交出去，不投。还在等上游（waiting_on
    非空）时也不投：动态照常记进时间线，最后一个上游完成时投一封信，agent 自己 issue_show 看等待期间的动态。

    例外是用户的评论（user_comment）：按上面的规则没有 agent 接手时（backlog、blocked、in_review、done、cancelled，
    没指派或指派给用户，在等上游），投给负责项目的 agent，由它判断关掉、派人还是回复；有被指派的 agent 接手时只投给
    它，同一件事只有一个 agent 负责。agent 的评论与字段修改不唤起 owner，避免 agent 之间来回唤醒。

    from_its_session 是在自己这个 issue 会话里动手的 agent（否则为 None）：它自己的动作不投回它正在做事的会话，
    避免自激循环。它在别的会话（如普通对话）里给自己建、派、转 in_progress，照常投进 issue 会话——issue 只在那里做。
    """
    agent = _assignee_recipient(issue)
    if agent is not None:
        return None if agent == from_its_session else agent
    return issue.owner if user_comment else None


def stalled(issue: Snapshot, assignee_state: WorkState | None) -> bool:
    """该有人做却没人在做：in_progress、不在等上游、指派给 agent，它在这个 issue 上的会话空闲或不存在。会话在等
    （后台任务、委派、到点唤醒、提问的回答）不算：等到了会接着做。界面与早报共用。"""
    return _assignee_recipient(issue) is not None and assignee_state in (None, WorkState.IDLE)


def ask_to_settle(issue: Snapshot, agent: str, state: WorkState | None) -> bool:
    """agent 在它这个 issue 会话里的一次运行结束后，要不要请它表态（记一条 UNSETTLED 并投给它）：issue 指派给它且因此
    停滞——它停下来时既没做完、会话也没在等什么。强制表态（用户定）：每次这样停下都问，直到它把状态改离 in_progress
    （做完、blocked），或让会话等着（后台任务、委派、到点唤醒）；只写评论不算表态。与 #107 的续跑不同：不重跑原任务，每次只投一封要它改状态的信。"""
    return issue.assignee == agent and stalled(issue, state)
