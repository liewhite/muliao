//! issue 给模型看的文本：issue_show 的完整内容、投进 issue 会话的信件、热历史里的一条动态。只读视图，展示名与本地时间在
//! 这里换。

use chrono_tz::Tz;
use muliao_assets::agents::AgentNames;

use crate::activity::{Activity, Happening};
use crate::graph::{Dependency, Links};
use crate::model::{Actor, Assignee, Issue};

/// 做事的人的展示名：用户是 `user`，系统是 `system`，agent 按 id 换成名字。
pub fn actor_name(actor: &Actor, names: &AgentNames) -> String {
    match actor {
        Actor::Agent(id) => names.name(id),
        other => other.as_str().to_owned(),
    }
}

/// 指派人的展示名：未指派是 `unassigned`，用户是 `user`，agent 按 id 换成名字。
pub fn assignee_name(assignee: Option<&Assignee>, names: &AgentNames) -> String {
    match assignee {
        None => "unassigned".into(),
        Some(Assignee::User) => Assignee::User.as_str().to_owned(),
        Some(Assignee::Agent(id)) => names.name(id),
    }
}

fn owner_name(owner: Option<&str>, names: &AgentNames) -> String {
    owner.map_or_else(|| "none".into(), |id| names.name(id))
}

fn issues(found: &[i64]) -> String {
    if found.is_empty() { "none".into() } else { found.iter().map(|i| format!("#{i}")).collect::<Vec<_>>().join(", ") }
}

fn dependencies(found: &[Dependency]) -> String {
    if found.is_empty() {
        return "none".into();
    }
    found.iter().map(|d| format!("#{} ({})", d.id, d.status)).collect::<Vec<_>>().join(", ")
}

fn indented(body: &str) -> String {
    format!("\n  {}", body.replace('\n', "\n  "))
}

/// 时间线上的一条给模型看的文本：本地时间（精确到分钟）、展示名。
pub fn render_activity(a: &Activity, names: &AgentNames, tz: Tz) -> String {
    let head = format!("- [{}]", a.created_at.with_timezone(&tz).format("%Y-%m-%d %H:%M"));
    let actor = actor_name(&a.actor, names);
    let kind = a.what.kind();
    match &a.what {
        Happening::Created => format!("{head} {actor} created the issue"),
        Happening::Comment { body } => format!("{head} {actor} commented:{}", indented(body)),
        Happening::Description => format!("{head} {actor} edited the description"),
        Happening::Title { change } => format!("{head} {actor} changed {kind}: {} -> {}", change.before, change.to),
        Happening::Status { change } => format!("{head} {actor} changed {kind}: {} -> {}", change.before, change.to),
        Happening::Priority { change } => format!("{head} {actor} changed {kind}: {} -> {}", change.before, change.to),
        Happening::Assignee { change } => format!(
            "{head} {actor} changed {kind}: {} -> {}",
            assignee_name(change.before.as_ref(), names),
            assignee_name(change.to.as_ref(), names)
        ),
        Happening::Owner { change } => format!(
            "{head} {actor} changed {kind}: {} -> {}",
            owner_name(change.before.as_deref(), names),
            owner_name(change.to.as_deref(), names)
        ),
        Happening::DependsOn { change } => {
            format!("{head} {actor} changed {kind}: {} -> {}", issues(&change.before), issues(&change.to))
        }
        Happening::Dependency { change } => {
            format!("{head} {actor} changed status of dependency #{}: {} -> {}", change.issue, change.before, change.to)
        }
        Happening::RunFailed { body } => format!("{head} {actor}'s run in its issue session failed: {body}"),
        Happening::Stopped { change } => {
            format!("{head} {actor} stopped {}'s conversation on this issue", names.name(&change.to))
        }
        // 怎么表态的指引在会话来源说明（缓存前缀）；这里只多一句「只写评论不算」作提醒
        Happening::Unsettled => format!(
            "{head} {actor}'s run in its issue session ended with the issue still `in_progress` and nothing pending to \
             wake it up: settle it now as the note on this conversation says (a comment alone does not settle it; you \
             are asked again every time you stop like this)"
        ),
    }
}

/// issue 的完整文本（issue_show）：概要、依赖、说明、整条时间线。
pub fn render_issue(issue: &Issue, timeline: &[Activity], links: &Links, names: &AgentNames, tz: Tz) -> String {
    let mut lines = vec![
        format!("# Issue #{}: {}", issue.id, issue.title),
        format!(
            "owner (project): {} | status: {} | priority: {} | assignee: {} | created by: {}",
            owner_name(issue.owner.as_deref(), names),
            issue.status,
            issue.priority,
            assignee_name(issue.assignee.as_ref(), names),
            actor_name(&issue.creator, names)
        ),
    ];
    if !links.depends_on.is_empty() || !links.blocks.is_empty() {
        let waiting = links.waiting_on();
        let waiting = if waiting.is_empty() {
            "none".to_owned()
        } else {
            waiting.iter().map(|i| format!("#{i}")).collect::<Vec<_>>().join(" ")
        };
        lines.push(format!(
            "depends on: {} | waiting on: {waiting} | blocks: {}",
            dependencies(&links.depends_on),
            dependencies(&links.blocks)
        ));
    }
    let description = issue.description.trim();
    lines.push(String::new());
    lines.push(if description.is_empty() { "(no description)".into() } else { description.to_owned() });
    lines.push(String::new());
    lines.push("## Activity".into());
    lines.extend(timeline.iter().map(|a| render_activity(a, names, tz)));
    lines.join("\n")
}

/// 投进 issue 会话的一封信：一行摘要加这次的动态，不随时间线变长。完整内容由 agent 自己 issue_show；怎么处理 issue 的说明
/// 在会话来源的固定说明里（[`crate::rules::SESSION_NOTE`]），不在信里重复。投递时上游都已完成，所以摘要列出上游及其状态，
/// 不列等待。
pub fn delivery_message(issue: &Issue, happened: &[Activity], links: &Links, names: &AgentNames, tz: Tz) -> String {
    let mut summary = vec![
        format!("#{}: {}", issue.id, issue.title),
        format!("status: {}", issue.status),
        format!("assignee: {}", assignee_name(issue.assignee.as_ref(), names)),
    ];
    if !links.depends_on.is_empty() {
        summary.push(format!("depends on: {}", dependencies(&links.depends_on)));
    }
    let happened: Vec<String> = happened.iter().map(|a| render_activity(a, names, tz)).collect();
    format!(
        "New activity on issue {}\n{}\n\nFull issue (description, dependencies, whole timeline): `issue_show({})`.",
        summary.join(" | "),
        happened.join("\n"),
        issue.id
    )
}
