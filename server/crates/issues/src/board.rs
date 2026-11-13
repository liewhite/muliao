//! issue 看板的门面（docs/architecture.md 第 21 节）：查询、修改命令、对会话事件与删除 agent 的反应。
//!
//! 每次修改在一个事务里经一个变更单元（[`crate::change`]）：这里的入口只描述改了什么（改哪些字段、记哪种动态、投不投），
//! 记时间线、投递、通知、放行下游都由单元统一结算。做事的人（[`By`]）是 agent（带它所在的会话）、用户或系统；agent 在自己
//! 这个 issue 会话里的动作不投回那个会话（避免自激），issue 只在它自己的会话里做，普通对话只建、只派。

use std::collections::BTreeMap;
use std::sync::Arc;

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use muliao_assets::agents::{agent_names_in, runnable_agent_in};
use muliao_assets::lifecycle::DeletionHook;
use muliao_platform::db::{Database, Tx};
use muliao_platform::errors::{DomainError, ErrorKind};
use muliao_platform::text::truncate;
use muliao_sessions::activity::WorkState;
use muliao_sessions::events::{BoxError, RunEnded, RunFailed, SessionDeleted, Subscribers};
use muliao_sessions::halt::Halted;
use muliao_sessions::inbound::SenderInbox;
use muliao_sessions::origins::SessionOrigin;
use muliao_sessions::tools::Stopper;
use rusqlite::Connection;
use serde::Serialize;

use crate::activity::{self, Activity, timeline_in};
use crate::change::{Changes, NotifyIn, Patch, Routing, check_assignee, checked_title, snapshot};
use crate::conversation;
use crate::error::{Error, Result, conflict, invalid};
use crate::graph::{Upstream, batch_label, check_batch, downstream_in, links_in, links_of_in, set_depends_on_in};
use crate::model::{self, Actor, Assignee, By, Issue, Priority, Status, agent_of, issue_in};
use crate::render::{render_activity, render_issue};
use crate::rules::{ask_to_settle, cannot_review, cannot_stop_into, handed_by, stalled, start_assignee};

/// 运行失败记进时间线的正文（错误原文）最多这么多字符，全文在会话页
const RUN_FAILED_BODY_MAX: usize = 500;

/// 谁在这个 issue 上做事、它在等谁（从会话领域与依赖图派生，不存）。
#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct IssueWork {
    /// 每个 agent 在这个 issue 上的会话：agent id -> 会话 id
    pub sessions: BTreeMap<String, String>,
    /// 被指派 agent 的 issue 会话的工作状态；没指派给 agent 或它还没有这个会话时为 null
    pub assignee_state: Option<WorkState>,
    /// in_progress、指派给 agent，它的 issue 会话却空闲或不存在：该有人做却没人在做
    pub stalled: bool,
    /// 它依赖的上游、依赖它的下游、还没完成（不是 done / cancelled）的上游：非空时还轮不到它
    pub depends_on: Vec<i64>,
    pub blocks: Vec<i64>,
    pub waiting_on: Vec<i64>,
    /// 已结束（done / cancelled）：不再挡着依赖它的 issue
    pub finished: bool,
    /// 用户能审核（通过或打回）：指派给用户、还没结束
    pub reviewable: bool,
    /// 用户能一键开工（指派给 owner 并进入 in_progress）：还在 backlog、有 owner
    pub startable: bool,
}

/// 看板上的一个 issue：它本身加上谁在做（HTTP 返回的形状）。
#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct BoardIssue {
    #[serde(flatten)]
    pub issue: Issue,
    #[serde(flatten)]
    pub work: IssueWork,
}

#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct IssueDetail {
    pub issue: BoardIssue,
    pub activities: Vec<Activity>,
}

/// 按条件列 issue；都不给是全部。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Filter {
    /// 只要这些状态；空是全部
    pub statuses: Vec<Status>,
    pub assignee: Option<Assignee>,
    /// 负责项目的 agent id
    pub owner: Option<String>,
}

/// 新建的一个 issue（一批里的一项）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct NewIssue {
    /// 负责项目的 agent id，必填
    pub owner: String,
    pub title: String,
    pub description: String,
    pub status: Status,
    pub priority: Priority,
    pub assignee: Option<Assignee>,
    /// 它依赖的上游：已有 issue 的编号，或同批另一项的 key
    pub depends_on: Vec<Upstream>,
    /// 同批别的项在 depends_on 里用它指这一项
    pub key: Option<String>,
}

/// [`Board::stop`] 的结果。
#[derive(Debug, Clone, PartialEq)]
pub struct Stopping {
    pub issue: Issue,
    /// 被指派的 agent（停的是它的会话）
    pub agent: String,
    /// 真的停了一个会话；它还没有这个 issue 的会话时为 false
    pub stopped: bool,
}

/// 用户在一段时间里对一个 issue 做的事（评论与属性变化），每条渲染成与 issue_show 同样的一行（热历史用）。
#[derive(Debug, Clone, PartialEq)]
pub struct UserActions {
    pub issue_id: i64,
    pub title: String,
    /// 此刻的状态
    pub status: Status,
    pub lines: Vec<String>,
}

/// rules 的 `cannot_*` 给出了拒绝原因时报 Conflict（用户看到的是过时的状态：连点、两台设备、通知过时）。
fn refuse(issue_id: i64, reason: Option<String>) -> Result<()> {
    match reason {
        Some(r) => Err(conflict(format!("issue #{issue_id} {r}"))),
        None => Ok(()),
    }
}

/// 一批（size 项）里第 n 项的错误带上它是哪一项；只建一个时原样交回。
pub(crate) fn in_batch<T>(size: usize, n: usize, key: Option<&str>, r: Result<T>) -> Result<T> {
    match r {
        Err(Error::Domain(d)) if size > 1 && matches!(d.kind, ErrorKind::InvalidInput | ErrorKind::NotFound) => {
            Err(DomainError::new(d.kind, format!("{}: {}", batch_label(n, key), d.message)).into())
        }
        other => other,
    }
}

/// 一批 issue 上谁在做事：查询次数与 issue 数无关。
fn work_in(c: &Connection, issues: &[Issue]) -> Result<BTreeMap<i64, IssueWork>> {
    let ids: Vec<i64> = issues.iter().map(|i| i.id).collect();
    let mut sessions = conversation::sessions_among(c, &ids)?;
    let keys: Vec<(i64, String)> =
        issues.iter().filter_map(|i| agent_of(i.assignee.as_ref()).map(|a| (i.id, a.to_owned()))).collect();
    let states = conversation::states_in(c, &keys)?;
    let mut links = links_in(c, &ids)?;
    let mut work = BTreeMap::new();
    for i in issues {
        let state = agent_of(i.assignee.as_ref()).and_then(|a| states[&(i.id, a.to_owned())]);
        let l = links.remove(&i.id).unwrap_or_default();
        let snap = snapshot(i, &l);
        work.insert(
            i.id,
            IssueWork {
                sessions: sessions.remove(&i.id).unwrap_or_default(),
                assignee_state: state,
                stalled: stalled(&snap, state),
                depends_on: l.depends_on.iter().map(|d| d.id).collect(),
                blocks: l.blocks.iter().map(|d| d.id).collect(),
                waiting_on: snap.waiting_on,
                finished: i.status.finished(),
                reviewable: cannot_review(i.status, i.assignee.as_ref()).is_none(),
                startable: start_assignee(i.status, i.owner.as_deref()).is_ok(),
            },
        );
    }
    Ok(work)
}

fn board_in(c: &Connection, issues: Vec<Issue>) -> Result<Vec<BoardIssue>> {
    let mut work = work_in(c, &issues)?;
    Ok(issues
        .into_iter()
        .map(|issue| {
            let work = work.remove(&issue.id).expect("work for every issue");
            BoardIssue { issue, work }
        })
        .collect())
}

/// 用户在 [start, end) 里对这些 agent 负责（owner）或被指派的 issue（None 为全部）做的事，按 issue 分组、按时间排序
/// （热历史 activity_overview 用）。按 issue 此刻的 owner / 指派人筛：当天被转给别人的 issue 算新的负责人的。
pub fn user_actions_in(
    c: &Connection,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
    agent_ids: Option<&[String]>,
    tz: Tz,
) -> Result<Vec<UserActions>> {
    // 时间经 rusqlite 的 chrono 转换传，和存下时同一种文字形式，按文字比较才对
    let mut values: Vec<Box<dyn rusqlite::ToSql>> =
        vec![Box::new(Actor::User.as_str().to_owned()), Box::new(start), Box::new(end)];
    let mut filter = "actor = ?1 AND created_at >= ?2 AND created_at < ?3".to_owned();
    if let Some(ids) = agent_ids {
        let marks: Vec<String> = ids
            .iter()
            .map(|id| {
                values.push(Box::new(id.clone()));
                format!("?{}", values.len())
            })
            .collect();
        let marks = marks.join(", ");
        filter.push_str(&format!(
            " AND issue_id IN (SELECT id FROM issues WHERE owner_agent_id IN ({marks}) OR assignee IN ({marks}))"
        ));
    }
    filter.push_str(" ORDER BY id");
    let rows = activity::query(c, &filter, rusqlite::params_from_iter(values))?;
    let names = agent_names_in(c)?;
    // 按 issue 分组，组的先后按各自第一条动态
    let mut grouped: Vec<(i64, Vec<Activity>)> = Vec::new();
    for a in rows {
        match grouped.iter_mut().find(|(id, _)| *id == a.issue_id) {
            Some((_, acts)) => acts.push(a),
            None => grouped.push((a.issue_id, vec![a])),
        }
    }
    grouped
        .into_iter()
        .map(|(id, acts)| {
            let issue = issue_in(c, id)?;
            Ok(UserActions {
                issue_id: id,
                title: issue.title,
                status: issue.status,
                lines: acts.iter().map(|a| render_activity(a, &names, tz)).collect(),
            })
        })
        .collect()
}

/// issue 看板。克隆共享同一份。
#[derive(Clone)]
pub struct Board {
    db: Arc<Database>,
    changes: Changes,
    stopper: Stopper,
}

impl Board {
    /// notify：在事务里发通知（组合根接到通知领域）；stopper：停止会话的入口（issue_stop 用）。
    pub fn new(db: Arc<Database>, inbox: SenderInbox, notify: NotifyIn, stopper: Stopper, tz: Tz) -> Self {
        Self { db, changes: Changes { inbox, notify, tz }, stopper }
    }

    pub(crate) fn db(&self) -> &Arc<Database> {
        &self.db
    }

    // ---- 对别的领域的反应 ----

    /// 登记对会话事件的订阅：运行结束时请停滞的 agent 表态、最终失败记进时间线、会话删除时时间线不再指向它。
    pub fn subscribe(&self, subscribers: &mut Subscribers) {
        let this = self.clone();
        subscribers.on_run_ended(move |tx, e| this.on_run_ended(tx, e).map_err(BoxError::from));
        let this = self.clone();
        subscribers.on_run_failed(move |tx, e| this.on_run_failed(tx, e).map_err(BoxError::from));
        subscribers.on_session_deleted(|tx, e: &SessionDeleted| {
            activity::forget_session_in(tx, &e.session_id).map_err(BoxError::from)
        });
    }

    /// issue 会话里的一次运行结束了：issue 指派给这个 agent、却因此停滞（还是 in_progress，会话空闲）时，以它的名义记一条
    /// 「没表态」并投进那个会话，请它表态。停在挂起的调用上、被用户停止的不问；会话已经又有事要做（信箱里还有信、在等后台
    /// 任务）时不算停滞，不问。其他来源的会话、已被删除的 issue 不处理。
    fn on_run_ended(&self, tx: &mut Tx<'_>, e: &RunEnded) -> Result<()> {
        let SessionOrigin::Issue { issue_id } = e.origin else { return Ok(()) };
        if e.stopped || !e.waiting_on.is_empty() {
            return Ok(());
        }
        let Some(issue) = model::find_in(tx, issue_id)? else { return Ok(()) };
        // 这个发送方给它开过不止一个会话时只认最新的：别的那个不是它在这个 issue 上的会话，不问
        if conversation::session_in(tx, issue_id, &e.agent_id)?.is_none_or(|s| s.id != e.session_id) {
            return Ok(());
        }
        let key = (issue_id, e.agent_id.clone());
        let state = conversation::states_in(tx, std::slice::from_ref(&key))?[&key];
        if !ask_to_settle(&snapshot(&issue, &links_of_in(tx, issue_id)?), &e.agent_id, state) {
            return Ok(());
        }
        // 记在它自己的 issue 会话名下，却要投进那个会话：直接投给它（已确认它就是该接手的人），不走「自己会话里的动作不
        // 投回」的规则
        let by = By::agent(&e.agent_id, &e.session_id);
        self.changes.open_in(tx, issue_id, &by, Routing::Direct(e.agent_id.clone()), |c| c.unsettled())?;
        tracing::info!(issue_id, run_id = e.run_id, "issue left unsettled, asked");
        Ok(())
    }

    /// issue 会话里的运行最终失败：以该 agent 的名义记进时间线（失败的是它的运行）。通知用户由通知领域做；只记、不投、不改
    /// 状态：投进会话就是自动重试，永久性的错误会陪它无限循环。之后有新动态时照常投递、唤醒它。
    fn on_run_failed(&self, tx: &mut Tx<'_>, e: &RunFailed) -> Result<()> {
        let SessionOrigin::Issue { issue_id } = e.origin else { return Ok(()) };
        if model::find_in(tx, issue_id)?.is_none() {
            tracing::info!(issue_id, run_id = e.run_id, "run failed on a deleted issue");
            return Ok(());
        }
        let by = By::agent(&e.agent_id, &e.session_id);
        let body = truncate(e.error.trim(), RUN_FAILED_BODY_MAX);
        self.changes.open_in(tx, issue_id, &by, Routing::RecordOnly, |c| c.run_failed(body))?;
        tracing::info!(issue_id, run_id = e.run_id, "issue run failure recorded");
        Ok(())
    }

    /// 删除 agent 的流程（在标记它的事务里）：指派给它、归它负责的 issue 改为未指派、无负责人（以系统身份记在时间线上，只记
    /// 不投）。它的会话由会话领域删除。
    pub fn deletion_hook(&self) -> DeletionHook {
        let this = self.clone();
        Arc::new(move |tx, agent_id| this.on_agent_deleting(tx, agent_id).map_err(to_assets))
    }

    fn on_agent_deleting(&self, tx: &mut Tx<'_>, agent_id: &str) -> Result<()> {
        let theirs = model::query(tx, "assignee = ?1 OR owner_agent_id = ?1 ORDER BY id", [agent_id])?;
        let by = By::system();
        for issue in theirs {
            self.changes.open_in(tx, issue.id, &by, Routing::RecordOnly, |c| {
                if agent_of(c.issue().assignee.as_ref()) == Some(agent_id) {
                    c.set_assignee(None)?;
                }
                if c.issue().owner.as_deref() == Some(agent_id) {
                    c.clear_deleted_owner()?;
                }
                Ok(())
            })?;
        }
        Ok(())
    }

    // ---- 查询 ----

    /// 按条件列出，最近更新的在前，带上谁在做。
    pub async fn list(&self, filter: Filter) -> Result<Vec<BoardIssue>> {
        self.db
            .read(move |c| {
                let mut conditions = vec!["1 = 1".to_owned()];
                let mut values: Vec<String> = Vec::new();
                if !filter.statuses.is_empty() {
                    let marks: Vec<String> = filter
                        .statuses
                        .iter()
                        .map(|s| {
                            values.push(s.as_str().into());
                            format!("?{}", values.len())
                        })
                        .collect();
                    conditions.push(format!("status IN ({})", marks.join(", ")));
                }
                if let Some(a) = &filter.assignee {
                    values.push(a.as_str().into());
                    conditions.push(format!("assignee = ?{}", values.len()));
                }
                if let Some(o) = &filter.owner {
                    values.push(o.clone());
                    conditions.push(format!("owner_agent_id = ?{}", values.len()));
                }
                let filter = format!("{} ORDER BY updated_at DESC, id DESC", conditions.join(" AND "));
                let found = model::query(c, &filter, rusqlite::params_from_iter(&values))?;
                board_in(c, found)
            })
            .await
    }

    /// 一个 issue 连同谁在做与整条时间线。
    pub async fn detail(&self, issue_id: i64) -> Result<IssueDetail> {
        self.db
            .read(move |c| {
                let issue = board_in(c, vec![issue_in(c, issue_id)?])?.remove(0);
                Ok(IssueDetail { issue, activities: timeline_in(c, issue_id)? })
            })
            .await
    }

    /// 一个 issue 连同谁在做（命令做完后返回给界面）。
    pub async fn get(&self, issue_id: i64) -> Result<BoardIssue> {
        self.db.read(move |c| Ok(board_in(c, vec![issue_in(c, issue_id)?])?.remove(0))).await
    }

    /// issue 的完整文本（issue_show）。
    pub async fn render(&self, issue_id: i64) -> Result<String> {
        let tz = self.changes.tz;
        self.db
            .read(move |c| {
                let issue = issue_in(c, issue_id)?;
                let timeline = timeline_in(c, issue_id)?;
                Ok(render_issue(&issue, &timeline, &links_of_in(c, issue_id)?, &agent_names_in(c)?, tz))
            })
            .await
    }

    // ---- 修改 ----

    /// 一次建一批 issue（按给出的顺序编号），可以互相依赖：depends_on 里的 key 指同批的另一项。整批一个事务，任何一项不合法
    /// 整批不建；全部建好、依赖连好之后才各开一个变更单元记「创建」、结算投递：先开工的子 issue 看得到同批的父 issue（blocks），
    /// 也不会在依赖补上之前就被投递。
    pub async fn create_many(&self, by: By, new: Vec<NewIssue>) -> Result<Vec<Issue>> {
        if new.is_empty() {
            return Err(invalid("no issues to create"));
        }
        let items: Vec<(Option<&str>, &[Upstream])> =
            new.iter().map(|i| (i.key.as_deref(), i.depends_on.as_slice())).collect();
        check_batch(&items)?;
        let changes = self.changes.clone();
        let created = self
            .db
            .write(move |tx| {
                let size = new.len();
                let now = Utc::now();
                let mut ids = Vec::new();
                for (n, item) in new.iter().enumerate() {
                    let id = in_batch(
                        size,
                        n,
                        item.key.as_deref(),
                        (|| {
                            check_assignee(tx, item.assignee.as_ref())?;
                            runnable_agent_in(tx, &item.owner)?;
                            let row = Issue {
                                id: 0,
                                title: checked_title(&item.title)?,
                                description: item.description.clone(),
                                status: item.status,
                                priority: item.priority,
                                owner: Some(item.owner.clone()),
                                assignee: item.assignee.clone(),
                                creator: by.actor.clone(),
                                created_at: now,
                                updated_at: now,
                            };
                            model::insert_in(tx, &row)
                        })(),
                    )?;
                    ids.push(id);
                }
                let keyed: BTreeMap<&str, i64> =
                    new.iter().zip(&ids).filter_map(|(i, id)| i.key.as_deref().map(|k| (k, *id))).collect();
                for (n, (item, id)) in new.iter().zip(&ids).enumerate() {
                    let upstream: Vec<i64> = item
                        .depends_on
                        .iter()
                        .map(|d| match d {
                            Upstream::Issue(i) => *i,
                            Upstream::Key(k) => keyed[k.as_str()],
                        })
                        .collect();
                    in_batch(size, n, item.key.as_deref(), set_depends_on_in(tx, *id, &upstream))?;
                }
                let mut created = Vec::new();
                for id in &ids {
                    let ((), issue) = changes.open_in(tx, *id, &by, Routing::ByRules, |c| c.created())?;
                    tracing::info!(issue_id = id, actor = by.actor.as_str(), "issue created");
                    created.push(issue);
                }
                Ok::<_, Error>(created)
            })
            .await?;
        Ok(created)
    }

    /// 只改给出的字段。每个真正变了的字段记一条时间线；issue 进入 done / cancelled 时由变更单元放行下游。
    pub async fn update(&self, by: By, issue_id: i64, patch: Patch) -> Result<Issue> {
        let changes = self.changes.clone();
        let issue = self
            .db
            .write(move |tx| {
                let ((), issue) = changes.open_in(tx, issue_id, &by, Routing::ByRules, |c| c.apply(patch))?;
                tracing::info!(issue_id, actor = by.actor.as_str(), status = %issue.status, "issue updated");
                Ok::<_, Error>(issue)
            })
            .await?;
        Ok(issue)
    }

    /// 用户审核通过交给自己的 issue：设为 done（有下游时照常放行）。
    pub async fn approve(&self, issue_id: i64) -> Result<Issue> {
        self.as_user(issue_id, move |c| {
            refuse(issue_id, cannot_review(c.issue().status, c.issue().assignee.as_ref()))?;
            c.set_status(Status::Done)
        })
        .await
    }

    /// 用户开工一个 backlog issue：指派给负责项目的 agent 并进入 in_progress，按规则投递。已不在 backlog（连点、两台设备）
    /// 或没有 owner 时拒绝。
    pub async fn start(&self, issue_id: i64) -> Result<Issue> {
        self.as_user(issue_id, move |c| {
            let issue = c.issue();
            let owner = start_assignee(issue.status, issue.owner.as_deref())
                .map_err(|r| conflict(format!("issue #{issue_id} {r}")))?
                .to_owned();
            c.apply(Patch {
                status: Some(Status::InProgress),
                assignee: Some(Some(Assignee::Agent(owner))),
                ..Patch::default()
            })
        })
        .await
    }

    /// 用户打回交给自己的 issue：理由记成评论，回到 in_progress，指派给把它交给用户的那个 agent。三条动态一起投进那个 agent 的
    /// issue 会话（一封信）。
    pub async fn reject(&self, issue_id: i64, reason: String) -> Result<Issue> {
        if reason.trim().is_empty() {
            return Err(invalid("the reason for sending an issue back must not be empty"));
        }
        self.as_user(issue_id, move |c| {
            let issue = c.issue().clone();
            refuse(issue_id, cannot_review(issue.status, issue.assignee.as_ref()))?;
            let timeline = c.timeline()?;
            let Some(agent) = handed_by(&issue.creator, &timeline).map(str::to_owned) else {
                return Err(conflict(format!(
                    "issue #{issue_id} was not handed to you by an agent: assign it to an agent yourself"
                )));
            };
            c.comment(reason)?;
            // 指派时查它还能不能运行：已删除的换成更明白的冲突（归档的本来就是冲突）
            let back = Patch {
                status: Some(Status::InProgress),
                assignee: Some(Some(Assignee::Agent(agent))),
                ..Patch::default()
            };
            match c.apply(back) {
                Err(Error::Domain(d)) if d.kind == ErrorKind::NotFound => Err(conflict(format!(
                    "the agent that handed issue #{issue_id} to you no longer exists: assign it to an agent yourself"
                ))),
                other => other,
            }
        })
        .await
    }

    /// 用户在界面上的一次修改（按规则投递）。
    async fn as_user(
        &self,
        issue_id: i64,
        f: impl FnOnce(&mut crate::change::IssueChange<'_>) -> Result<()> + Send + 'static,
    ) -> Result<Issue> {
        let changes = self.changes.clone();
        let issue = self
            .db
            .write(move |tx| {
                let ((), issue) = changes.open_in(tx, issue_id, &By::user(), Routing::ByRules, f)?;
                Ok::<_, Error>(issue)
            })
            .await?;
        tracing::info!(issue_id, status = %issue.status, "issue changed by the user");
        Ok(issue)
    }

    /// 叫停被指派 agent 在这个 issue 上的会话（issue_stop，#272）：停掉它（同用户点停止，连同它开出去的 thread），把 issue
    /// 改成 status，时间线记「停掉了」与状态变更。有 message 时记成评论，连同这次的动态直接投进被停下的会话跑一轮（收尾或
    /// 改方向），不论新状态是什么。
    ///
    /// 没有 agent 指派人时 Conflict（没有会话可停，改状态用 update）；在要停的那个会话里调用 InvalidInput。指派人还没有这个
    /// 会话（或它刚被删除）时没有可停的：只改状态、记评论，按规则投递，不为收尾新建会话。
    ///
    /// 先停后改：停止返回时停止事务已提交，之后投的信不会被它清掉。两个事务之间指派人被改了也不改目标：记的、收尾消息投的都
    /// 是被停下的那个 agent。从被停会话开出去的 thread 里调用时，那个 thread 随停止被逐层停掉，可能在第二个事务之前
    /// 就被取消：会话停了而状态没改，调用方看不到结果（契约之外的用法，不另拦）。
    pub async fn stop(&self, by: By, issue_id: i64, status: Status, message: Option<String>) -> Result<Stopping> {
        if message.as_deref().is_some_and(|m| m.trim().is_empty()) {
            return Err(invalid("message must not be empty"));
        }
        if let Some(reason) = cannot_stop_into(status, message.is_some()) {
            return Err(invalid(reason));
        }
        let (agent, target) = self
            .db
            .read(move |c| {
                let issue = issue_in(c, issue_id)?;
                let Some(agent) = agent_of(issue.assignee.as_ref()).map(str::to_owned) else {
                    return Err(conflict(format!(
                        "issue #{issue_id} is not assigned to an agent: there is no conversation to stop (use \
                         issue_update to change it)"
                    )));
                };
                let target = conversation::session_in(c, issue_id, &agent)?;
                Ok((agent, target))
            })
            .await?;
        if target.as_ref().is_some_and(|t| Some(&t.id) == by.session_id.as_ref()) {
            return Err(invalid(format!(
                "this is the conversation of issue #{issue_id} itself: change the issue with issue_update instead"
            )));
        }
        let stopped = match &target {
            Some(t) => self.stopper.stop(&t.id).await? == Halted::Stopped,
            None => false,
        };
        let routing = if stopped && message.is_some() { Routing::Direct(agent.clone()) } else { Routing::ByRules };
        let (changes, who) = (self.changes.clone(), agent.clone());
        let issue = self
            .db
            .write(move |tx| {
                let ((), issue) = changes.open_in(tx, issue_id, &by, routing, |c| {
                    if stopped {
                        c.stopped(&who)?;
                    }
                    c.set_status(status)?;
                    if let Some(m) = message {
                        c.comment(m)?;
                    }
                    Ok(())
                })?;
                tracing::info!(issue_id, actor = by.actor.as_str(), agent = %who, stopped, %status, "issue conversation stopped");
                Ok::<_, Error>(issue)
            })
            .await?;
        Ok(Stopping { issue, agent, stopped })
    }

    pub async fn comment(&self, by: By, issue_id: i64, body: String) -> Result<Activity> {
        if body.trim().is_empty() {
            return Err(invalid("comment must not be empty"));
        }
        let changes = self.changes.clone();
        self.db
            .write(move |tx| {
                let (a, _) = changes.open_in(tx, issue_id, &by, Routing::ByRules, |c| c.comment(body))?;
                tracing::info!(issue_id, actor = by.actor.as_str(), "issue commented");
                Ok(a)
            })
            .await
    }

    /// 删除 issue（用户的操作）。依赖它的下游少了这个上游：各开一个变更单元记依赖变化，按规则投递（可能因此不再等待）。
    pub async fn delete(&self, issue_id: i64) -> Result<()> {
        let changes = self.changes.clone();
        self.db
            .write(move |tx| {
                issue_in(tx, issue_id)?;
                let downstream = downstream_in(tx, issue_id)?;
                let before = links_in(tx, &downstream)?;
                model::delete_in(tx, issue_id)?;
                for down in &downstream {
                    let old: Vec<i64> = before[down].depends_on.iter().map(|d| d.id).collect();
                    changes.open_in(tx, *down, &By::user(), Routing::ByRules, |c| c.upstream_deleted(old, issue_id))?;
                }
                tracing::info!(issue_id, ?downstream, "issue deleted");
                Ok(())
            })
            .await
    }
}

fn to_assets(e: Error) -> muliao_assets::error::Error {
    match e {
        Error::Domain(d) => d.into(),
        Error::Db(d) => d.into(),
        Error::Assets(a) => a,
        other => std::io::Error::other(other.to_string()).into(),
    }
}
