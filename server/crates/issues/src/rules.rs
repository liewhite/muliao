//! issue 的纯规则（docs/architecture.md 第 21 节）：动态投给谁、一次改动带出什么（通知用户、放行下游）、什么算停滞、哪些
//! 操作此刻做不了（返回拒绝原因）。依赖图在 [`crate::graph`]，给模型看的文本在 [`crate::render`]。

use muliao_platform::ids::AgentId;
use muliao_sessions::activity::WorkState;

use crate::activity::{Activity, Happening};
use crate::model::{Actor, Assignee, Recorded, Status, agent_of};

/// issue 会话在指令里的说明：怎么处理一个 issue（会话级、不变，进缓存前缀；信件里不重复）。组合根交给会话领域
/// （`OriginNotes`）。
pub const SESSION_NOTE: &str = "This conversation is dedicated to one issue on the muliao issue board that you work on or own, and is \
continued each time the issue needs you again: while it is `in_progress`, every change to it \
(comments, status, assignee, edits, a dependency finishing) except those you make here arrives here as a short message \
carrying only that new activity. Read the full issue (description, dependencies, whole timeline) with \
`issue_show` before you start, and again whenever the message is not enough. An issue that depends on \
others is delivered only once all of them are `done` or `cancelled`; a cancelled dependency may mean \
this issue is no longer needed. Work on it here, then record the outcome on the issue with the `issue_*` \
tools: comment with what you did or found (`issue_comment`), and move it on with `issue_update`. \
Never end your turn with the issue left `in_progress` and nothing said about what comes next: before you \
stop, either finish it; or keep this conversation waiting on something that will wake it up here: a \
background job (`run_shell` with `background: true`, e.g. a loop polling for what you wait for), work \
delegated with `report_back: true`, or `wake_me` to be woken at a time you choose; or set `blocked`, \
assignee `user`, with a comment on what only the user can do. A comment alone does not count: when you stop \
with the issue `in_progress` and nothing pending here, the platform asks you again. When you have finished, check \
`issue_show` again: if other issues depend on this one (listed under `blocks`), it is a step of a larger piece \
of work, so set it `done` yourself and the next steps start; otherwise do not set `done` yourself: set \
status `in_review` and assignee `user`, and the user moves it to `done` after reviewing. Hand it to \
another agent by changing the assignee. A user comment on an issue no agent is working on (not `in_progress`, \
not assigned to an agent, or still waiting on dependencies) arrives here when you are \
its owner, even if it is not assigned to you: decide whether to close it, assign it to someone, or reply. \
Nobody is watching this conversation: the user reads the issue, not this conversation.";

/// issue_stop 叫停之后 issue 可以进入的状态：停下不是做完，不能交审（in_review）或完成（done）；in_progress 是打断后改方向。
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum StopStatus {
    Backlog,
    Blocked,
    Cancelled,
    InProgress,
}

impl StopStatus {
    pub fn status(self) -> Status {
        match self {
            StopStatus::Backlog => Status::Backlog,
            StopStatus::Blocked => Status::Blocked,
            StopStatus::Cancelled => Status::Cancelled,
            StopStatus::InProgress => Status::InProgress,
        }
    }
}

/// 叫停之后 issue 怎样：停在 backlog、blocked、cancelled（可以带一句话记成评论、投给被停下的会话收尾），或留在 in_progress
/// 改方向（必须带消息告诉 agent 现在做什么）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StopTo {
    Backlog(Option<String>),
    Blocked(Option<String>),
    Cancelled(Option<String>),
    Redirect(String),
}

impl StopTo {
    /// 工具交来的状态与消息；空消息、留在 in_progress 却没带消息时是原因。
    pub fn new(status: StopStatus, message: Option<String>) -> Result<Self, String> {
        if message.as_deref().is_some_and(|m| m.trim().is_empty()) {
            return Err("message must not be empty".into());
        }
        Ok(match (status, message) {
            (StopStatus::Backlog, m) => StopTo::Backlog(m),
            (StopStatus::Blocked, m) => StopTo::Blocked(m),
            (StopStatus::Cancelled, m) => StopTo::Cancelled(m),
            (StopStatus::InProgress, Some(m)) => StopTo::Redirect(m),
            (StopStatus::InProgress, None) => {
                return Err("give a message when the issue stays in_progress: it tells the agent what to do now".into());
            }
        })
    }

    pub fn status(&self) -> Status {
        match self {
            StopTo::Backlog(_) => Status::Backlog,
            StopTo::Blocked(_) => Status::Blocked,
            StopTo::Cancelled(_) => Status::Cancelled,
            StopTo::Redirect(_) => Status::InProgress,
        }
    }

    pub fn message(&self) -> Option<&str> {
        match self {
            StopTo::Backlog(m) | StopTo::Blocked(m) | StopTo::Cancelled(m) => m.as_deref(),
            StopTo::Redirect(m) => Some(m),
        }
    }
}

/// 投递与停滞判定看的那部分 issue 状态（动作之后的）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Snapshot {
    pub status: Status,
    pub assignee: Option<Assignee>,
    /// 负责项目的 agent id；负责的 agent 被删除后为 None
    pub owner: Option<AgentId>,
    /// 还没完成的上游；非空时这个 issue 在等上游，还轮不到它
    pub waiting_on: Vec<i64>,
}

/// 用户此刻不能审核（通过或打回）这个 issue 的原因（接在「issue #N」之后）；能审核为 None。只审当前指派给用户、还没结束
/// 的：已经审过、又被改派或取消（连点、两台设备、通知过时）时拒绝，不覆盖别人刚做的事。
pub fn cannot_review(status: Status, assignee: Option<&Assignee>) -> Option<String> {
    if assignee != Some(&Assignee::User) {
        let who = assignee.map_or("unassigned", Assignee::as_str);
        return Some(format!("is not assigned to you (assignee: {who}), nothing to review"));
    }
    if status.finished() {
        return Some(format!("is already {status}, nothing to review"));
    }
    None
}

/// 用户一键开工（指派给 owner 并进入 in_progress）时指派给谁；不能开工时是原因。只开工还在 backlog、有负责项目的 agent 的。
pub fn start_assignee(status: Status, owner: Option<&AgentId>) -> Result<&AgentId, String> {
    let owner = owner.ok_or("has no owner: assign it to an agent yourself")?;
    if status != Status::Backlog {
        return Err(format!("is already {status}, not in the backlog"));
    }
    Ok(owner)
}

/// 这次的动态里有外来的输入（用户的评论）：没有 agent 接手时也唤起 owner（见 [`recipient`]）。
pub fn from_outside(actor: &Actor, happened: &[Activity]) -> bool {
    *actor == Actor::User && happened.iter().any(|a| matches!(a.what, Happening::Comment { .. }))
}

/// 这次是 agent 把 issue 交给了用户（建时就指派给用户，或把指派人改成用户）：返回这个 agent（以它的名义通知用户），否则
/// None。用户、系统自己改的不通知。
pub fn handed_to_user<'a>(actor: &'a Actor, assignee: Option<&Assignee>, happened: &[Activity]) -> Option<&'a AgentId> {
    let agent = actor.agent()?;
    happened
        .iter()
        .any(|a| match &a.what {
            Happening::Created => assignee == Some(&Assignee::User),
            Happening::Assignee { change } => change.to == Some(Assignee::User),
            _ => false,
        })
        .then_some(agent)
}

/// 这次的状态变更让 issue 进入 done / cancelled（下游因此少等一个上游）：返回进入前的状态（原文），否则 None。
pub fn finishing(happened: &[Activity]) -> Option<Recorded> {
    happened.iter().find_map(|a| match &a.what {
        Happening::Status { change } => (!change.before.finished() && change.to.finished()).then_some(change.before),
        _ => None,
    })
}

/// 把当前指派给用户的 issue 交给用户的 agent（用户打回时指派回给它）：最近一条指派动态的 actor（它把指派人改成了用户）；
/// 从未改过指派人时是创建者（创建时就指派给了用户）。这次交接是用户自己或系统做的时为 None。
pub fn handed_by<'a>(creator: &'a Actor, timeline: &'a [Activity]) -> Option<&'a AgentId> {
    let last = timeline.iter().rev().find(|a| matches!(a.what, Happening::Assignee { .. }));
    last.map_or(creator, |a| &a.actor).agent()
}

/// 该由被指派的 agent 接手时就是它：in_progress、指派给 agent、不在等上游。
fn assignee_recipient(issue: &Snapshot) -> Option<&AgentId> {
    let agent = agent_of(issue.assignee.as_ref())?;
    (issue.status == Status::InProgress && issue.waiting_on.is_empty()).then_some(agent)
}

/// issue 上的一条动态（创建、评论、任何字段的修改）投给谁：动作之后处于 in_progress（包括这次动作让它进入）时，投给被
/// 指派的 agent；backlog 是以后再做，in_review / done 等是已经交出去，不投。还在等上游时也不投：动态照常记进时间线，最后
/// 一个上游完成时投一封信，agent 自己 issue_show 看等待期间的动态。
///
/// 例外是外来的输入（from_outside：用户的评论）：按上面的规则没有 agent 接手时，投给负责项目的 agent，由它判断关掉、派人
/// 还是回复；有被指派的 agent 接手时只投给它。agent 的评论与字段修改不唤起 owner，避免 agent 之间来回唤醒。
///
/// from_its_session 是在自己这个 issue 会话里动手的 agent：它自己的动作不投回它正在做事的会话，避免自激循环。它在别的会话
/// （如普通对话）里给自己建、派、转 in_progress，照常投进 issue 会话——issue 只在那里做。
pub fn recipient(issue: &Snapshot, from_outside: bool, from_its_session: Option<&AgentId>) -> Option<AgentId> {
    if let Some(agent) = assignee_recipient(issue) {
        return (Some(agent) != from_its_session).then(|| agent.to_owned());
    }
    if from_outside { issue.owner.clone() } else { None }
}

/// 该有人做却没人在做：in_progress、不在等上游、指派给 agent，它在这个 issue 上的会话空闲或不存在。会话在等（后台任务、
/// 委派、到点唤醒、提问的回答）不算：等到了会接着做。界面与 issue_list 共用。
pub fn stalled(issue: &Snapshot, assignee_state: Option<WorkState>) -> bool {
    assignee_recipient(issue).is_some() && matches!(assignee_state, None | Some(WorkState::Idle))
}

/// agent 在它这个 issue 会话里的一次运行结束后，要不要请它表态：issue 指派给它且因此停滞——它停下来时既没做完、会话也
/// 没在等什么。强制表态（用户定）：每次这样停下都问，直到它把状态改离 in_progress，或让会话等着；只写评论不算表态。不重跑
/// 原任务，每次只投一封要它改状态的信。
pub fn ask_to_settle(issue: &Snapshot, agent: &AgentId, state: Option<WorkState>) -> bool {
    agent_of(issue.assignee.as_ref()) == Some(agent) && stalled(issue, state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::activity::Change;
    use chrono::Utc;

    fn snap(status: Status, assignee: Option<Assignee>, waiting_on: Vec<i64>) -> Snapshot {
        Snapshot { status, assignee, owner: Some(AgentId::new("own")), waiting_on }
    }

    fn agent(id: &str) -> Option<Assignee> {
        Some(Assignee::Agent { agent_id: AgentId::new(id) })
    }

    fn act(actor: Actor, what: Happening) -> Activity {
        Activity { id: 0, issue_id: 1, created_at: Utc::now(), actor, session_id: None, what }
    }

    #[test]
    fn recipient_follows_status_assignee_dependencies_and_origin() {
        let work = snap(Status::InProgress, agent("a"), vec![]);
        assert_eq!(recipient(&work, false, None).as_ref().map(AgentId::as_str), Some("a"));
        // 自己 issue 会话里的动作不投回
        assert_eq!(recipient(&work, false, Some(&AgentId::new("a"))), None);
        assert_eq!(recipient(&work, false, Some(&AgentId::new("b"))).as_ref().map(AgentId::as_str), Some("a"));
        // 有接手的 agent 时，用户评论也只投给它
        assert_eq!(recipient(&work, true, None).as_ref().map(AgentId::as_str), Some("a"));
        // 在等上游：不投；用户的评论唤起 owner
        let waiting = snap(Status::InProgress, agent("a"), vec![3]);
        assert_eq!(recipient(&waiting, false, None), None);
        assert_eq!(recipient(&waiting, true, None).as_ref().map(AgentId::as_str), Some("own"));
        for status in [Status::Backlog, Status::InReview, Status::Blocked, Status::Done, Status::Cancelled] {
            assert_eq!(recipient(&snap(status, agent("a"), vec![]), false, None), None);
            assert_eq!(
                recipient(&snap(status, agent("a"), vec![]), true, None).as_ref().map(AgentId::as_str),
                Some("own")
            );
        }
        assert_eq!(recipient(&snap(Status::InProgress, Some(Assignee::User), vec![]), false, None), None);
    }

    #[test]
    fn stalled_only_when_the_assignee_should_work_and_its_session_is_idle() {
        let work = snap(Status::InProgress, agent("a"), vec![]);
        assert!(stalled(&work, None));
        assert!(stalled(&work, Some(WorkState::Idle)));
        assert!(!stalled(&work, Some(WorkState::Waiting)));
        assert!(!stalled(&work, Some(WorkState::Running)));
        assert!(!stalled(&snap(Status::InProgress, agent("a"), vec![2]), None));
        assert!(!stalled(&snap(Status::Blocked, agent("a"), vec![]), None));
        assert!(ask_to_settle(&work, &AgentId::new("a"), Some(WorkState::Idle)));
        assert!(!ask_to_settle(&work, &AgentId::new("b"), Some(WorkState::Idle)));
    }

    #[test]
    fn review_start_and_stop_refusals() {
        assert!(cannot_review(Status::InReview, Some(&Assignee::User)).is_none());
        assert!(cannot_review(Status::Done, Some(&Assignee::User)).is_some());
        assert!(cannot_review(Status::InReview, None).is_some());
        assert_eq!(start_assignee(Status::Backlog, Some(&AgentId::new("a"))).map(AgentId::as_str), Ok("a"));
        assert!(start_assignee(Status::Backlog, None).is_err());
        assert!(start_assignee(Status::Blocked, Some(&AgentId::new("a"))).is_err());
        assert!(StopTo::new(StopStatus::InProgress, None).is_err());
        assert!(StopTo::new(StopStatus::Blocked, Some(" ".into())).is_err());
        assert_eq!(StopTo::new(StopStatus::InProgress, Some("改做 X".into())), Ok(StopTo::Redirect("改做 X".into())));
        assert_eq!(StopTo::new(StopStatus::Cancelled, None).map(|s| s.status()), Ok(Status::Cancelled));
        // 停下不是做完：交审、完成不在可选的状态里
        assert!(serde_json::from_str::<StopStatus>("\"done\"").is_err());
        assert!(serde_json::from_str::<StopStatus>("\"in_review\"").is_err());
    }

    #[test]
    fn what_a_change_brings() {
        let a = Actor::Agent { agent_id: AgentId::new("a") };
        let handed = [act(a.clone(), Happening::Assignee { change: Change::new(None, Some(Assignee::User)) })];
        assert_eq!(handed_to_user(&a, Some(&Assignee::User), &handed).map(AgentId::as_str), Some("a"));
        assert_eq!(handed_to_user(&Actor::User, Some(&Assignee::User), &handed), None);
        let created = [act(a.clone(), Happening::Created)];
        assert_eq!(handed_to_user(&a, Some(&Assignee::User), &created).map(AgentId::as_str), Some("a"));
        assert_eq!(handed_to_user(&a, agent("b").as_ref(), &created), None);
        let done =
            [act(a.clone(), Happening::Status { change: Change::new(Status::InProgress.into(), Status::Done.into()) })];
        assert_eq!(finishing(&done), Some(Recorded::Now(Status::InProgress)));
        let reopened =
            [act(a.clone(), Happening::Status { change: Change::new(Status::Done.into(), Status::Cancelled.into()) })];
        assert_eq!(finishing(&reopened), None);
        let comment = [act(Actor::User, Happening::Comment { body: "x".into() })];
        assert!(from_outside(&Actor::User, &comment));
        assert!(!from_outside(&a, &comment));
        // 交给用户的是最近一条指派动态的 actor；没有就是创建者
        let creator = Actor::Agent { agent_id: AgentId::new("c") };
        assert_eq!(handed_by(&creator, &[]).map(AgentId::as_str), Some("c"));
        assert_eq!(handed_by(&creator, &handed).map(AgentId::as_str), Some("a"));
        let by_user = [act(Actor::User, Happening::Assignee { change: Change::new(None, Some(Assignee::User)) })];
        assert_eq!(handed_by(&creator, &by_user), None);
    }
}
