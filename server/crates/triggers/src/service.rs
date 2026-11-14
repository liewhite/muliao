//! 触发器的管理与触发（docs/architecture.md 第 14 节）。
//!
//! - 管理（HTTP 与工具共用）：建、改、删、列、按名字找、「立即运行」。改配置、重新启用由 [`NewConfig::configure`] 重新推导；
//!   换检查、改配置、重新启用是人工干预，清掉脚本的出错。
//! - 触发只有一个出口（[`TriggerService::fire_activated`]，激活机制用；「立即运行」[`TriggerService::run`] 同一条路）：先看
//!   触发器自己能不能触发（停用、agent 归档或正在删除），有前置检查就在事务外跑，再在一个事务里认账并投信——记下触发与
//!   投信在同一事务，恰好一次。每次触发新开触发器 agent 的一个会话，信没有用户在等（处理它的运行不推送，要通知时 agent
//!   调 notify_user）。
//! - 结束（finished_at）：不会再触发了。已结束的不在默认列表、不能改或运行，可删；行与 id 保留，它开过的会话仍查得到来源。
//!
//! 任何改动提交后通知激活机制（[`TriggerService::watch`]），它们按最新的行重新对账。

use muliao_platform::ids::{AgentId, SessionId};
use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use chrono_tz::Tz;
use muliao_assets::agents::runnable_agent_in;
use muliao_assets::lifecycle::DeletionHook;
use muliao_assets::secrets::SecretUse;
use muliao_notifications::{Notifications, Notify};
use muliao_platform::db::{Database, Tx};
use muliao_sessions::activity::{WorkState, states_of_in};
use muliao_sessions::inbound::{Refusal, Refused, SenderInbox, SenderMail, SenderTarget};
use muliao_sessions::origins::Sender;
use muliao_workspace::commands::AgentCommands;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::watch;

use crate::faults::{FaultSource, Faults, TriggerFault, local_minute, reset};
use crate::kinds::{KindView, Mode, NewConfig};
use crate::precheck::{Failed, Precheck, PrecheckRunner, alert_letter, decide, failed, passed_in, set_in};
use crate::store::{
    Listing, NewTrigger, Trigger, baseline_in, delete_in, insert_in, list_in, load_in, named_in, save_in,
};
use crate::stream::Hosting;
use muliao_platform::errors::{Error, Result, conflict, invalid, not_found};

/// 事件内容（流式命令的输出、前置检查的输出）原样进 prompt，过大的应由来源裁剪或改为传引用
pub const EVENT_MAX_BYTES: usize = 256 * 1024;

/// 触发器开出的会话在指令里的说明（会话级、不变；组合根合进 `OriginNotes`）
pub const SESSION_NOTE: &str = "This run was started by a trigger (a schedule, or an external event shown inside \
     <event>); nobody is watching. Each firing starts a fresh conversation, so keep anything later firings need in \
     memory or workspace files.";

const NAME_MAX: usize = 64;

/// 名称：小写字母、数字与 `-`，以字母或数字开头，最长 64。
pub fn validate_name(name: &str) -> Result<()> {
    let ok = !name.is_empty()
        && name.len() <= NAME_MAX
        && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && !name.starts_with('-');
    if ok {
        Ok(())
    } else {
        Err(invalid(format!("invalid trigger name {name:?}: must match ^[a-z0-9][a-z0-9-]{{0,63}}$")))
    }
}

pub fn event_prompt(instructions: &str, payload: Option<&str>) -> String {
    match payload {
        None => instructions.to_owned(),
        Some(p) => format!("{}\n\n<event>\n{p}\n</event>", instructions.trim()),
    }
}

/// 交给 agent 的说明：不能是空的。只经 [`Prompt::new`] 构造（反序列化也走它）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(into = "String", try_from = "String")]
#[schema(value_type = String)]
pub struct Prompt(String);

impl Prompt {
    pub fn new(text: impl Into<String>) -> std::result::Result<Self, String> {
        let text = text.into();
        if text.trim().is_empty() {
            return Err("trigger prompt must not be empty".into());
        }
        Ok(Self(text))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<String> for Prompt {
    type Error = String;

    fn try_from(text: String) -> std::result::Result<Self, String> {
        Self::new(text)
    }
}

impl From<Prompt> for String {
    fn from(p: Prompt) -> String {
        p.0
    }
}

/// 上一次触发开出的会话还没做完时它在干什么（不会是 idle）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum Busy {
    Running,
    Waiting,
}

impl Busy {
    pub fn as_str(self) -> &'static str {
        match self {
            Busy::Running => "running",
            Busy::Waiting => "waiting",
        }
    }
}

/// 上一次触发开出的会话还没做完时：它的 id 与在干什么。从没触发过、会话已删除、idle 时 None。不是 idle 就还有没做完的事：
/// 在运行或排着运行，或在等后台任务、委派、到点唤醒、用户回答——等着的结果回来会自动接着做，再开一个会重叠（#627）。
/// 定时据此跳过，流式据此让事件排队。
pub(crate) fn busy_previous_in(c: &Connection, t: &Trigger) -> Result<Option<(SessionId, Busy)>> {
    let Some(session_id) = t.last_session_id.clone() else { return Ok(None) };
    let state = states_of_in(c, std::slice::from_ref(&session_id))?.remove(&session_id);
    Ok(match state {
        Some(WorkState::Running) => Some((session_id, Busy::Running)),
        Some(WorkState::Waiting) => Some((session_id, Busy::Waiting)),
        Some(WorkState::Idle) | None => None,
    })
}

/// 挡路的会话在等待（可能永远等不到）时通知用户，每个挡路会话只通知一次（记在 `waiting_notified`，重启后仍记得）。
/// 在运行的不通知：运行偶尔长过一个周期是常态。定时与流式共用；通知的内容由各自给出。调用方写回这一行。
pub(crate) fn notify_waiting_in(
    tx: &mut Tx<'_>,
    notifications: &Notifications,
    t: &mut Trigger,
    session_id: &SessionId,
    busy: Busy,
    notice: impl FnOnce(&Trigger) -> Notify,
) -> Result<()> {
    if busy == Busy::Running || t.waiting_notified.as_ref() == Some(session_id) {
        return Ok(());
    }
    notifications.notify_in(tx, &notice(t))?;
    t.waiting_notified = Some(session_id.clone());
    Ok(())
}

/// 一次触发投出去的信：新开的会话。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fired {
    pub session_id: SessionId,
}

/// 触发器自己的原因让这次没触发。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Skip {
    Disabled,
    /// 已删除，或它的 agent 不在了、正在删除
    Deleted,
    /// 它的 agent 已归档：触发器保持启用，取消归档后照常触发
    Archived,
}

/// 一次触发的结果。
#[derive(Debug, Clone, PartialEq, Eq)]
enum Outcome {
    Fired(Fired),
    /// 前置检查判定这次没事（或只记了基线），或检查期间触发器被改了、结果作废
    Nothing,
    Skipped(Skip, String),
    CheckFailed(String),
}

/// 触发的第一个事务的结果：已经有结果了，或要先在事务外跑前置检查。
enum First {
    Done(Outcome),
    /// previous：changed 检查上一次的输出（基线）
    Check {
        agent_id: AgentId,
        precheck: Precheck,
        previous: Option<String>,
    },
}

/// 谁在触发：决定前置检查失败怎么处理。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum By {
    /// 用户或 agent 的「立即运行」：检查失败只报给调用方，不记不告警（见 [`TriggerService::run`]）
    Run,
    /// 激活机制（到期扫描、流式）：没人等结果，记下并告警
    Activation,
}

/// 触发器的读模型（HTTP 层与工具共用）：行、它的配置与运行状态、上一次触发开出的、还在的会话。
#[derive(Debug, Clone)]
pub struct TriggerView {
    pub trigger: Trigger,
    pub kind: KindView,
    /// 上一次触发开出的会话，已删除时 None（`trigger.last_session_id` 是记下的原值）
    pub live_session_id: Option<SessionId>,
}

/// 新建一个触发器。
#[derive(Debug, Clone)]
pub struct NewSpec {
    pub name: String,
    pub agent_id: AgentId,
    pub prompt: Prompt,
    pub config: NewConfig,
    pub precheck: Option<Precheck>,
}

/// 前置检查怎么改。
#[derive(Debug, Clone, Default, PartialEq)]
pub enum PrecheckChange {
    #[default]
    Keep,
    Remove,
    Set(Precheck),
}

/// 修改：只改给出的字段。config 不带种类（种类不能改），按已存的种类解析。
#[derive(Debug, Clone, Default)]
pub struct Update {
    pub prompt: Option<Prompt>,
    pub config: Option<Value>,
    pub enabled: Option<bool>,
    pub precheck: PrecheckChange,
}

struct Inner {
    db: Arc<Database>,
    tz: Tz,
    commands: Arc<AgentCommands>,
    precheck: PrecheckRunner,
    inbox: SenderInbox,
    faults: Faults,
    hosting: Hosting,
    changed: watch::Sender<()>,
}

/// 克隆共享同一份。
#[derive(Clone)]
pub struct TriggerService(Arc<Inner>);

impl TriggerService {
    pub fn new(
        db: Arc<Database>,
        tz: Tz,
        commands: Arc<AgentCommands>,
        precheck_timeout: Duration,
        inbox: SenderInbox,
        faults: Faults,
        hosting: Hosting,
    ) -> Self {
        let precheck = PrecheckRunner::new(commands.clone(), precheck_timeout, EVENT_MAX_BYTES);
        let changed = watch::channel(()).0;
        Self(Arc::new(Inner { db, tz, commands, precheck, inbox, faults, hosting, changed }))
    }

    pub(crate) fn db(&self) -> &Arc<Database> {
        &self.0.db
    }

    pub(crate) fn faults(&self) -> &Faults {
        &self.0.faults
    }

    /// 任何触发器变化（提交后）都会让它变为 changed：激活机制据此重新对账。
    pub fn watch(&self) -> watch::Receiver<()> {
        self.0.changed.subscribe()
    }

    fn changed(&self) {
        self.0.changed.send_replace(());
    }

    // ---- 查询 ----

    pub async fn list(&self, listing: Listing) -> Result<Vec<Trigger>> {
        self.0.db.read(move |c| list_in(c, &listing)).await
    }

    pub async fn get(&self, trigger_id: i64) -> Result<Trigger> {
        self.0.db.read(move |c| load_in(c, trigger_id)?.ok_or_else(|| missing(trigger_id))).await
    }

    /// 按名称找还会触发的那个：名称只在未结束的之间唯一，已结束的只能按 id 指。这个名字只剩已结束的时说清楚它已结束。
    pub async fn get_by_name(&self, name: &str) -> Result<Trigger> {
        let name = name.to_owned();
        self.0
            .db
            .read(move |c| {
                let same = named_in(c, &name)?;
                if let Some(live) = same.iter().find(|t| t.finished_at.is_none()) {
                    return Ok(live.clone());
                }
                if same.is_empty() {
                    return Err(not_found(format!("trigger {name:?} not found")));
                }
                // 按名称的操作（含删除）都指不到已结束的：说清楚它已是历史、无需清理
                Err(conflict(format!(
                    "trigger {name:?} has finished and will not fire again; it is kept as history (see \
                     trigger_list(finished=true)), so there is nothing to change, run or delete"
                )))
            })
            .await
    }

    /// 读模型：各自的配置与运行状态，以及上一次触发开出的会话还在不在（一次读）。
    pub async fn views(&self, triggers: Vec<Trigger>) -> Result<Vec<TriggerView>> {
        let hosting = self.0.hosting.clone();
        self.0
            .db
            .read(move |c| {
                let ids: Vec<SessionId> = triggers.iter().filter_map(|t| t.last_session_id.clone()).collect();
                let states = states_of_in(c, &ids)?;
                Ok::<_, Error>(
                    triggers
                        .into_iter()
                        .map(|t| {
                            let alive = t.last_session_id.clone().filter(|id| states.contains_key(id));
                            TriggerView { kind: KindView::of(&t, &hosting), trigger: t, live_session_id: alive }
                        })
                        .collect(),
                )
            })
            .await
    }

    pub async fn view(&self, t: Trigger) -> Result<TriggerView> {
        Ok(self.views(vec![t]).await?.remove(0))
    }

    // ---- 管理 ----

    /// 前置检查与流式命令都是 agent 自己写的命令：建或改时就确认这些 secret 能交给它的命令（含 allow_shell），配置错误在第一
    /// 现场报出。与运行时起命令是同一步（`AgentCommands::prepare`），核对与实际不会分叉。
    async fn check_secrets(&self, agent_id: &AgentId, keys: &[String]) -> Result<()> {
        self.0.commands.prepare(agent_id, keys, SecretUse::AgentCommand).await?;
        Ok(())
    }

    pub async fn create(&self, spec: NewSpec) -> Result<Trigger> {
        validate_name(&spec.name)?;
        let mode = spec.config.configure(self.0.tz, Utc::now())?;
        let mut keys = mode.secrets().to_vec();
        keys.extend(spec.precheck.iter().flat_map(|p| p.secrets.clone()));
        self.check_secrets(&spec.agent_id, &keys).await?;
        let new =
            NewTrigger { name: spec.name, agent_id: spec.agent_id, prompt: spec.prompt, mode, precheck: spec.precheck };
        let created = self
            .0
            .db
            .write(move |tx| {
                // 正在删除的 agent 按不存在处理（否则删除流程清理过之后又建的触发器没人认领）；归档的拒绝（它不能运行）
                runnable_agent_in(tx, &new.agent_id)?;
                insert_in(tx, &new)?.ok_or_else(|| conflict(format!("trigger {:?} already exists", new.name)))
            })
            .await?;
        self.changed();
        let kind = created.mode.kind().name();
        tracing::info!(trigger = %created.name, kind, agent = %created.agent_id, "trigger created");
        Ok(created)
    }

    /// 在一个事务里只改给出的字段。换检查、改配置或重新启用清掉脚本的出错。
    pub async fn update(&self, trigger_id: i64, update: Update) -> Result<Trigger> {
        let tz = self.0.tz;
        // 核对 secret 要读 vault，不能放进下面的事务：先按此刻的行算出会生效的配置。中间有并发修改时核对按的是旧行，
        // 后果只是走回运行时的失败路径（与建好后 secret 才被删一样），有意不加锁
        let current = self.manageable(trigger_id).await?;
        let config = update.config.map(|c| NewConfig::parse(current.mode.kind(), c)).transpose()?;
        if matches!(update.precheck, PrecheckChange::Set(_)) || config.is_some() || update.enabled == Some(true) {
            let mut keys = match &update.precheck {
                PrecheckChange::Set(p) => p.secrets.clone(),
                PrecheckChange::Keep | PrecheckChange::Remove => Vec::new(),
            };
            if let Some(mode) = rederive(&current, config.clone(), update.enabled, tz)? {
                keys.extend_from_slice(mode.secrets());
            }
            self.check_secrets(&current.agent_id, &keys).await?;
        }
        let updated = self
            .0
            .db
            .write(move |tx| {
                let mut t = manageable_in(tx, trigger_id)?;
                if let Some(mode) = rederive(&t, config, update.enabled, tz)? {
                    t.mode = mode;
                    reset(&mut t);
                }
                match update.precheck {
                    PrecheckChange::Keep => {}
                    PrecheckChange::Remove => {
                        set_in(tx, &mut t, None)?;
                        reset(&mut t);
                    }
                    PrecheckChange::Set(p) => {
                        set_in(tx, &mut t, Some(p))?;
                        reset(&mut t);
                    }
                }
                if let Some(prompt) = update.prompt {
                    t.prompt = prompt.into();
                }
                if let Some(enabled) = update.enabled {
                    t.enabled = enabled;
                }
                save_in(tx, &t)?;
                Ok::<_, Error>(t)
            })
            .await?;
        self.changed();
        tracing::info!(trigger = %updated.name, enabled = updated.enabled, "trigger updated");
        Ok(updated)
    }

    async fn manageable(&self, trigger_id: i64) -> Result<Trigger> {
        self.0.db.read(move |c| manageable_in(c, trigger_id)).await
    }

    pub async fn delete(&self, trigger_id: i64) -> Result<()> {
        let deleted = self.0.db.write(move |tx| delete_in(tx, trigger_id)).await?;
        if !deleted {
            return Err(missing(trigger_id));
        }
        self.changed();
        tracing::info!(trigger_id, "trigger deleted");
        Ok(())
    }

    /// 删除 agent 的流程：删掉它的全部触发器（含已结束的），提交后通知激活机制（杀掉它的流式命令）。
    pub fn deletion_hook(&self) -> DeletionHook {
        let this = self.clone();
        Arc::new(move |tx: &mut Tx<'_>, agent_id: &AgentId| {
            tx.execute("DELETE FROM triggers WHERE agent_id = ?1", [agent_id])?;
            let this = this.clone();
            tx.on_commit(move || this.changed());
            Ok(())
        })
    }

    // ---- 触发 ----

    /// 用户或 agent 的「立即运行一次」（不带事件内容）：管理操作，不消耗一次性触发器预定的那一次。有前置检查时同样先跑，
    /// 可以用来测试整条链路；检查判定没事时返回 None。检查失败按输入不对报给调用方（改了脚本再试），不记在触发器上、不告警：agent
    /// 修脚本时用它验证，告警会另开会话重复处理；记下了又会压住之后自动触发时的告警（只在新出现时告警）。
    pub async fn run(&self, trigger_id: i64) -> Result<Option<Fired>> {
        self.manageable(trigger_id).await?;
        match self.fire(trigger_id, None, By::Run).await? {
            Outcome::Fired(f) => Ok(Some(f)),
            Outcome::Nothing => Ok(None),
            Outcome::Skipped(Skip::Disabled | Skip::Archived, detail) => Err(conflict(detail)),
            Outcome::Skipped(Skip::Deleted, detail) => Err(not_found(detail)),
            Outcome::CheckFailed(error) => Err(invalid(error)),
        }
    }

    /// 激活机制（到期扫描、流式）的触发：没有调用方等着，按结果逐种处理。不看是否已结束：按到期触发的在调用前已消耗了这次
    /// 到期，一次性的那时已经结束。
    /// - 前置检查失败：已记成出错并告警；脚本的故障往往是暂时的，保持启用。
    /// - 它的 agent 已归档：跳过，保持启用；触发器若已结束（这是它最后一次），告警说任务没做（agent 收不到，退给用户）。
    /// - 停用或删除：只记日志。
    pub async fn fire_activated(&self, trigger_id: i64, payload: Option<String>) -> Result<()> {
        match self.fire(trigger_id, payload, By::Activation).await? {
            Outcome::Fired(_) | Outcome::Nothing | Outcome::CheckFailed(_) => {}
            Outcome::Skipped(why, detail) => {
                tracing::info!(trigger_id, detail, "activated fire skipped");
                if why == Skip::Archived {
                    self.finished_unrun(trigger_id).await?;
                }
            }
        }
        Ok(())
    }

    /// 已结束的触发器最后一次没跑成（agent 已归档）：任务再也不会做了，告警。没结束的只是跳过这一次，不告警。
    async fn finished_unrun(&self, trigger_id: i64) -> Result<()> {
        let faults = self.0.faults.clone();
        self.0
            .db
            .write(move |tx| {
                let Some(t) = load_in(tx, trigger_id)?.filter(|t| t.finished_at.is_some()) else { return Ok(()) };
                let letter = format!(
                    "Your trigger `{}` came due for its last time while you were archived, so its task was NOT done \
                     and it has finished. Its prompt was:\n<prompt>\n{}\n</prompt>",
                    t.name, t.prompt
                );
                faults.alert_in(tx, &t, "未执行：它的 agent 已归档", letter, &t.prompt)
            })
            .await
    }

    async fn fire(&self, trigger_id: i64, payload: Option<String>, by: By) -> Result<Outcome> {
        let this = self.clone();
        let event = payload.clone();
        let first = self
            .0
            .db
            .write(move |tx| {
                let Some(mut t) = load_in(tx, trigger_id)? else {
                    let detail = format!("trigger {trigger_id} not found");
                    return Ok::<_, Error>(First::Done(Outcome::Skipped(Skip::Deleted, detail)));
                };
                // 停用、收不了信的 agent 不跑前置检查、不触发；检查期间被归档的，投信时由收信入口拒绝
                if let Some(outcome) = skip_in(tx, &t)? {
                    return Ok(First::Done(outcome));
                }
                match t.precheck.clone() {
                    None => Ok(First::Done(this.deliver_in(tx, &mut t, event.as_deref())?)),
                    Some(precheck) => {
                        let previous = baseline_in(tx, trigger_id)?;
                        Ok(First::Check { agent_id: t.agent_id.clone(), precheck, previous })
                    }
                }
            })
            .await?;
        let (agent_id, precheck, previous) = match first {
            First::Done(outcome) => return Ok(outcome),
            First::Check { agent_id, precheck, previous } => (agent_id, precheck, previous),
        };
        let max_bytes = self.0.precheck.max_bytes();
        // diff 在写事务之外算：输出再大也不占着全平台的写
        let checked = self.0.precheck.run(&agent_id, &precheck, payload.as_deref()).await?.and_then(|output| {
            let decision = decide(&precheck, &output, previous.as_deref(), max_bytes)?;
            Ok((output, decision))
        });
        if let (Err(Failed(error)), By::Run) = (&checked, by) {
            return Ok(Outcome::CheckFailed(error.clone()));
        }
        let this = self.clone();
        self.0
            .db
            .write(move |tx| {
                // 检查期间触发器被删除、停用或改了检查：这次的结果（成功或失败）都作废
                let Some(mut t) =
                    load_in(tx, trigger_id)?.filter(|t| t.enabled && t.precheck.as_ref() == Some(&precheck))
                else {
                    tracing::info!(trigger_id, "trigger changed during its precheck, result discarded");
                    return Ok(Outcome::Nothing);
                };
                let (output, decision) = match checked {
                    Ok(done) => done,
                    Err(Failed(error)) => return this.check_failed_in(tx, &mut t, &precheck, error, by),
                };
                passed_in(tx, &mut t, &precheck, &output, Utc::now())?;
                match decision.event {
                    None => {
                        tracing::debug!(trigger = %t.name, baseline = decision.baseline, "precheck: nothing to do");
                        save_in(tx, &t)?;
                        Ok(Outcome::Nothing)
                    }
                    Some(event) => this.deliver_in(tx, &mut t, Some(&event)),
                }
            })
            .await
    }

    /// 检查失败：激活机制触发的记成出错（从正常变为出错时告警），「立即运行」的只报给调用方。
    fn check_failed_in(
        &self,
        tx: &mut Tx<'_>,
        t: &mut Trigger,
        precheck: &Precheck,
        error: String,
        by: By,
    ) -> Result<Outcome> {
        match by {
            By::Run => {}
            By::Activation => {
                failed(t, Utc::now());
                let fault = TriggerFault { source: FaultSource::Precheck, error: error.clone() };
                self.0.faults.report_in(tx, t, fault, |t| alert_letter(t, precheck, &error))?;
                save_in(tx, t)?;
            }
        }
        Ok(Outcome::CheckFailed(error))
    }

    /// 新开触发器 agent 的一个会话交出这次事件：记下触发与投信在同一事务，恰好一次。agent 收不了信（检查期间被归档、
    /// 正在删除）时这次跳过。
    fn deliver_in(&self, tx: &mut Tx<'_>, t: &mut Trigger, payload: Option<&str>) -> Result<Outcome> {
        let now = Utc::now();
        let mail = SenderMail {
            sender: Sender::Trigger { trigger_id: t.id },
            content: event_prompt(&t.prompt, payload),
            session: SenderTarget::New {
                agent_id: t.agent_id.clone(),
                title: format!("{} · {}", t.name, local_minute(now, self.0.tz)),
            },
        };
        let session_id = match self.0.inbox.receive_in(tx, mail)? {
            Ok(accepted) => accepted.session_id,
            Err(refused) => return Ok(skipped(t, refused)),
        };
        (t.last_fired_at, t.last_session_id) = (Some(now), Some(session_id.clone()));
        save_in(tx, t)?;
        tracing::info!(trigger = %t.name, kind = t.mode.kind().name(), %session_id, "trigger fired");
        Ok(Outcome::Fired(Fired { session_id }))
    }
}

fn missing(trigger_id: i64) -> Error {
    not_found(format!("trigger {trigger_id} not found"))
}

/// 管理操作（改、启用、立即运行）的前提：这个触发器还会触发。已结束的只能查看和删除。
fn manageable_in(c: &Connection, trigger_id: i64) -> Result<Trigger> {
    let t = load_in(c, trigger_id)?.ok_or_else(|| missing(trigger_id))?;
    if t.finished_at.is_some() {
        return Err(conflict(format!(
            "trigger {:?} has finished and will not fire again; it can only be viewed or deleted",
            t.name
        )));
    }
    Ok(t)
}

/// 这次修改要不要重新推导：配置变了、或停用后重新启用（定时的下次时间从现在算、流式重起进程）才要；规范化后
/// 没变的配置（界面只改说明时也带着原配置）不算改动，运行状态留着。
fn rederive(t: &Trigger, config: Option<NewConfig>, enabled: Option<bool>, tz: Tz) -> Result<Option<Mode>> {
    let reenabled = enabled == Some(true) && !t.enabled;
    let input = match config {
        Some(c) => c,
        None if reenabled => t.mode.input(),
        None => return Ok(None),
    };
    let derived = input.configure(tz, Utc::now())?;
    Ok((reenabled || !derived.same_config(&t.mode)).then_some(derived))
}

/// 触发器这次跑不跑（前置检查之前）：停用的不跑；它的 agent 收不了信的不跑前置检查（能不能收信由会话领域回答）。
fn skip_in(c: &Connection, t: &Trigger) -> Result<Option<Outcome>> {
    if !t.enabled {
        return Ok(Some(Outcome::Skipped(Skip::Disabled, format!("trigger {:?} is disabled", t.name))));
    }
    Ok(SenderInbox::refusal_in(c, &t.agent_id)?.map(|r| skipped(t, r)))
}

/// 它的 agent 收不了信：这次跳过（归档的保持启用，取消归档后照常触发；不在了的按删除算）。
fn skipped(t: &Trigger, refused: Refused) -> Outcome {
    let why = match refused.reason {
        Refusal::Archived => Skip::Archived,
        Refusal::Gone => Skip::Deleted,
    };
    Outcome::Skipped(why, format!("trigger {:?}: {}", t.name, refused.detail))
}
