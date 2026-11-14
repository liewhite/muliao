//! issue 的变更单元：一次修改带出的全部后果（记动态、投递、通知、放行下游）只在这里发生。
//!
//! 修改入口（[`crate::board::Board`] 的各个命令、事件处理）只描述改了什么：在调用方的事务里用 [`Changes::open_in`] 取到
//! issue，期间只经单元改字段（`set_*` / [`IssueChange::apply`]）、记动态（[`IssueChange::record`]）；闭包正常返回时统一结算：
//!
//! - 投递与通知，按开单元时声明的 [`Routing`]：`ByRules` 按 [`crate::rules::recipient`] 判定投给谁，`Direct(agent)` 直接
//!   投给这个 agent（issue_stop 的收尾消息、请停滞的 agent 表态），`RecordOnly` 只记时间线（运行失败、删除 agent 的清理）；
//! - 放行下游：这次的状态变更让 issue 进入 done / cancelled 时，每个下游各开一个单元记一条上游完成，按同一条规则投递——不再
//!   等别的上游时，这就是让它开工的那封信。
//!
//! 闭包返回错误时不结算（事务随之回滚）。一次修改什么都没改（没记动态）时什么都不发。issue 的字段只在本模块赋值。

use chrono_tz::Tz;
use muliao_assets::agents::{agent_names_in, runnable_agent_in};
use muliao_notifications::{Notifications, Notify};
use muliao_platform::db::Tx;
use muliao_platform::ids::{AgentId, IssueId};
use muliao_sessions::inbound::{Refusal, Refused, SenderInbox};
use rusqlite::Connection;

use crate::activity::{Activity, Arrival, Change, DependencyChange, Happening, record_in, timeline_in};
use crate::conversation;
use crate::graph::{Links, downstream_in, links_of_in, set_depends_on_in};
use crate::model::{Actor, Assignee, By, Issue, Priority, Status, agent_of, issue_in, save_in};
use crate::render::delivery_message;
use crate::rules::{Snapshot, finishing, from_outside, handed_to_user, recipient};
use muliao_platform::errors::{Result, invalid};

const TITLE_MAX: usize = 300;

/// 一次修改结算时投给谁。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Routing {
    /// 按 [`crate::rules::recipient`] 判定
    ByRules,
    /// 直接投给这个 agent，不按规则判定：issue_stop 的收尾消息投给被停下的 agent，不论 issue 的新状态；运行结束后请停滞的
    /// agent 表态，它记在那个 issue 会话名下，按「自己会话里的动作不投回」会被挡掉
    Direct(AgentId),
    /// 只记时间线：不投递、不通知、不放行下游
    RecordOnly,
}

/// 要改的字段，没给的（None）不变。assignee：`Some(None)` 取消指派；depends_on 整体替换（空列表去掉全部）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Patch {
    pub owner: Option<AgentId>,
    pub title: Option<String>,
    pub description: Option<String>,
    pub status: Option<Status>,
    pub priority: Option<Priority>,
    pub assignee: Option<Option<Assignee>>,
    pub depends_on: Option<Vec<IssueId>>,
}

pub(crate) fn checked_title(title: &str) -> Result<String> {
    let title = title.trim();
    if title.is_empty() {
        return Err(invalid("issue title must not be empty"));
    }
    if title.chars().count() > TITLE_MAX {
        return Err(invalid(format!("issue title is longer than {TITLE_MAX} characters")));
    }
    Ok(title.to_owned())
}

/// 指派给 agent 时它要能运行：正在删除的按不存在处理（否则删除流程清理过之后又指派给它，没人认领），归档的拒绝（它接不到信）。
pub(crate) fn check_assignee(c: &Connection, assignee: Option<&Assignee>) -> Result<()> {
    if let Some(agent) = agent_of(assignee) {
        runnable_agent_in(c, agent)?;
    }
    Ok(())
}

/// 投递与停滞判定看的那部分状态：issue 本身加它还在等的上游。
pub(crate) fn snapshot(issue: &Issue, links: &Links) -> Snapshot {
    Snapshot {
        status: issue.status,
        assignee: issue.assignee.clone(),
        owner: issue.owner.clone(),
        waiting_on: links.waiting_on(),
    }
}

/// 一次修改：一个 issue、做事的人、这期间记下的动态。由 [`Changes::open_in`] 给出。
pub(crate) struct IssueChange<'c> {
    c: &'c Connection,
    issue: Issue,
    by: &'c By,
    recorded: Vec<Activity>,
}

impl IssueChange<'_> {
    /// 当前的 issue（只读：改字段经下面的方法）。
    pub fn issue(&self) -> &Issue {
        &self.issue
    }

    /// 这个 issue 的整条时间线（含这次已记下的）。
    pub fn timeline(&self) -> Result<Vec<Activity>> {
        timeline_in(self.c, self.issue.id)
    }

    /// 记一条动态（时间线上多一条就是 issue 变了：updated_at 只在这里更新）。只在本模块里用：属性变化必须经
    /// set_* 与字段一起改，对外只给下面几种不改字段的记法。
    fn record(&mut self, what: Happening) -> Result<Activity> {
        let a = record_in(self.c, self.issue.id, self.by, what)?;
        self.issue.updated_at = a.created_at;
        self.recorded.push(a.clone());
        Ok(a)
    }

    // 不改字段的动态

    pub fn created(&mut self) -> Result<()> {
        self.record(Happening::Created).map(drop)
    }

    pub fn comment(&mut self, body: String) -> Result<Activity> {
        self.record(Happening::Comment { body })
    }

    pub fn run_failed(&mut self, body: String) -> Result<()> {
        self.record(Happening::RunFailed { body }).map(drop)
    }

    pub fn unsettled(&mut self) -> Result<()> {
        self.record(Happening::Unsettled).map(drop)
    }

    /// 停掉了 agent 在这个 issue 上的会话。
    pub fn stopped(&mut self, agent_id: &AgentId) -> Result<()> {
        self.record(Happening::Stopped { change: Arrival { before: (), to: agent_id.to_owned() } }).map(drop)
    }

    /// 一个上游被删除：它已从依赖表里随之删掉，这里只记下前后的上游列表。
    pub fn upstream_deleted(&mut self, before: Vec<IssueId>, deleted: IssueId) -> Result<()> {
        let to = before.iter().copied().filter(|&i| i != deleted).collect();
        self.record(Happening::DependsOn { change: Change::new(before, to) }).map(drop)
    }

    // 每个 set_* 只在值真的变了时改字段、记一条动态

    pub fn set_title(&mut self, title: &str) -> Result<()> {
        let title = checked_title(title)?;
        if title != self.issue.title {
            let before = std::mem::replace(&mut self.issue.title, title.clone());
            self.record(Happening::Title { change: Change::new(before, title) })?;
        }
        Ok(())
    }

    pub fn set_description(&mut self, description: &str) -> Result<()> {
        if description != self.issue.description {
            self.issue.description = description.to_owned();
            self.record(Happening::Description)?;
        }
        Ok(())
    }

    pub fn set_depends_on(&mut self, depends_on: &[IssueId]) -> Result<()> {
        if let Some((before, to)) = set_depends_on_in(self.c, self.issue.id, depends_on)? {
            self.record(Happening::DependsOn { change: Change::new(before, to) })?;
        }
        Ok(())
    }

    pub fn set_status(&mut self, status: Status) -> Result<()> {
        if status != self.issue.status {
            let before = std::mem::replace(&mut self.issue.status, status);
            self.record(Happening::Status { change: Change::new(before.into(), status.into()) })?;
        }
        Ok(())
    }

    pub fn set_owner(&mut self, owner: &AgentId) -> Result<()> {
        if Some(owner) != self.issue.owner.as_ref() {
            runnable_agent_in(self.c, owner)?;
            self.change_owner(Some(owner.to_owned()))?;
        }
        Ok(())
    }

    /// 负责的 agent 被删除（系统清理）：不再有 owner。
    pub fn clear_deleted_owner(&mut self) -> Result<()> {
        if self.issue.owner.is_some() {
            self.change_owner(None)?;
        }
        Ok(())
    }

    fn change_owner(&mut self, owner: Option<AgentId>) -> Result<()> {
        let before = std::mem::replace(&mut self.issue.owner, owner.clone());
        self.record(Happening::Owner { change: Change::new(before, owner) }).map(drop)
    }

    pub fn set_priority(&mut self, priority: Priority) -> Result<()> {
        if priority != self.issue.priority {
            let before = std::mem::replace(&mut self.issue.priority, priority);
            self.record(Happening::Priority { change: Change::new(before, priority) })?;
        }
        Ok(())
    }

    pub fn set_assignee(&mut self, assignee: Option<Assignee>) -> Result<()> {
        if assignee != self.issue.assignee {
            check_assignee(self.c, assignee.as_ref())?;
            let before = std::mem::replace(&mut self.issue.assignee, assignee.clone());
            self.record(Happening::Assignee { change: Change::new(before, assignee) })?;
        }
        Ok(())
    }

    /// 改给出的字段，时间线上的顺序固定。
    pub fn apply(&mut self, p: Patch) -> Result<()> {
        if let Some(t) = &p.title {
            self.set_title(t)?;
        }
        if let Some(d) = &p.description {
            self.set_description(d)?;
        }
        if let Some(d) = &p.depends_on {
            self.set_depends_on(d)?;
        }
        if let Some(s) = p.status {
            self.set_status(s)?;
        }
        if let Some(o) = &p.owner {
            self.set_owner(o)?;
        }
        if let Some(pr) = p.priority {
            self.set_priority(pr)?;
        }
        if let Some(a) = p.assignee {
            self.set_assignee(a)?;
        }
        Ok(())
    }
}

/// 开变更单元、在退出时结算（见模块说明）。克隆共享同一份。
#[derive(Clone)]
pub(crate) struct Changes {
    pub inbox: SenderInbox,
    pub notifications: Notifications,
    pub tz: Tz,
}

impl Changes {
    /// 在调用方的事务里取 issue（不存在报 NotFound），开一次修改；f 正常返回时写回 issue 并按 routing 结算。返回 f 的结果与
    /// 修改后的 issue。
    pub fn open_in<T>(
        &self,
        tx: &mut Tx<'_>,
        issue_id: IssueId,
        by: &By,
        routing: Routing,
        f: impl FnOnce(&mut IssueChange<'_>) -> Result<T>,
    ) -> Result<(T, Issue)> {
        let (value, issue, happened) = {
            let mut change = IssueChange { c: tx, issue: issue_in(tx, issue_id)?, by, recorded: Vec::new() };
            let value = f(&mut change)?;
            (value, change.issue, change.recorded)
        };
        if !happened.is_empty() {
            save_in(tx, &issue)?;
            self.settle_in(tx, &issue, by, &happened, routing)?;
        }
        Ok((value, issue))
    }

    fn settle_in(
        &self,
        tx: &mut Tx<'_>,
        issue: &Issue,
        by: &By,
        happened: &[Activity],
        routing: Routing,
    ) -> Result<()> {
        let to = match routing {
            Routing::RecordOnly => return Ok(()),
            Routing::Direct(agent) => Some(agent),
            Routing::ByRules => None,
        };
        self.post_in(tx, issue, by, happened, to)?;
        if let Some(was) = finishing(happened) {
            let downstream = downstream_in(tx, issue.id)?;
            for down in &downstream {
                let change = DependencyChange { issue: issue.id, before: was, to: issue.status.into() };
                self.open_in(tx, *down, by, Routing::ByRules, |d| {
                    d.record(Happening::Dependency { change }).map(drop)
                })?;
            }
            if !downstream.is_empty() {
                tracing::info!(issue_id = %issue.id, status = %issue.status, ?downstream, "issue unblocked downstream");
            }
        }
        Ok(())
    }

    /// 发出这次动态带出的消息：agent 把 issue 交给用户时推送通知；把动态投给该收到的 agent（或 to 直接给出的），进它在这个
    /// issue 上唯一的会话。
    fn post_in(
        &self,
        tx: &mut Tx<'_>,
        issue: &Issue,
        by: &By,
        happened: &[Activity],
        to: Option<AgentId>,
    ) -> Result<()> {
        if handed_to_user(&by.actor, issue.assignee.as_ref(), happened).is_some() {
            // 点开是 issue（在那里通过或打回）；也带上它交出 issue 的那个会话
            let notice = Notify {
                title: format!("#{} 指派给你：{}", issue.id, issue.title),
                body: format!("状态 {}", issue.status),
                session_id: by.session_id.clone(),
                link: Some(crate::route(issue.id)),
            };
            self.notifications.notify_in(tx, &notice)?;
        }
        let links = links_of_in(tx, issue.id)?;
        let to = match to {
            Some(agent) => Some(agent),
            None => {
                let its_session = from_its_session_in(tx, issue.id, by)?;
                recipient(&snapshot(issue, &links), from_outside(&by.actor, happened), its_session)
            }
        };
        let Some(to) = to else { return Ok(()) };
        let names = agent_names_in(tx)?;
        let letter = delivery_message(issue, happened, &links, &names, self.tz);
        let mail = conversation::mail(issue.id, &to, format!("#{} {}", issue.id, issue.title), letter);
        match self.inbox.receive_in(tx, mail)? {
            Ok(_) => {
                tracing::info!(issue_id = %issue.id, agent = %to, actor = by.actor.as_str(), "issue activity delivered")
            }
            Err(Refused { reason: Refusal::Archived, detail }) if by.actor == Actor::User => {
                // 已有 issue 的指派人或 owner 后来被归档：动态照常记进时间线（不让用户在这类 issue 上的评论、改动报错），不投；
                // 用户自己的改动、评论没人接要当场告诉他（只有他能取消归档或改指派）
                let notice = Notify {
                    title: format!("#{} 的动态没交给 {}：它已归档", issue.id, names.name(&to)),
                    body: "取消归档后它用 issue_show 能看到这些动态；也可以把 issue 改指派给别的 agent。".into(),
                    session_id: None,
                    link: Some(crate::route(issue.id)),
                };
                self.notifications.notify_in(tx, &notice)?;
                tracing::info!(issue_id = %issue.id, %detail, "issue activity not delivered");
            }
            // 别人的动态（agent 的）投不进归档的 agent 只记日志：in_progress 的由看板的「停滞」标出。Gone：agent 正在删除，
            // 删除流程会清理它在 issue 上的指派与 owner
            Err(Refused { reason: Refusal::Archived | Refusal::Gone, detail }) => {
                tracing::info!(issue_id = %issue.id, %detail, "issue activity not delivered");
            }
        }
        Ok(())
    }
}

/// 动作是不是 agent 在它自己这个 issue 会话里做的：是则返回它（这次动态不投回那里）。
fn from_its_session_in<'b>(c: &Connection, issue_id: IssueId, by: &'b By) -> Result<Option<&'b AgentId>> {
    let (Some(agent), Some(session_id)) = (by.actor.agent(), by.session_id.as_ref()) else { return Ok(None) };
    let its = conversation::session_in(c, issue_id, agent)?;
    Ok(its.is_some_and(|s| &s.id == session_id).then_some(agent))
}
