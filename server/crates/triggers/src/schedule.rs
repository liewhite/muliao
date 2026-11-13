//! 定时触发：config = `{"cron": "<5 段 cron 表达式>"}`，按服务端配置的时区解释。
//!
//! - 下次时间记在 state 的 `next_run_at`；到点先推进到下次（消耗这次到期），再触发（[`crate::due`]）。
//! - 停机期间错过的只补一次，然后从当前时间算下一次；停用后重新启用，下次时间从启用时算起，不补暂停期间的。
//! - 不重叠：上一次触发开出的会话还没做完（[`busy_previous_in`]）时跳过本次；前置检查还在跑、还没有会话时由扫描循环记下的
//!   「在触发」挡住（只消耗不触发）。
//! - 跳过要看得见：连续跳过记在 state 的 `skipped`（次数、从何时起、挡路的会话在运行还是在等待），作为状态给界面与模型看；
//!   挡路的会话在等待（后台任务、委派、用户回答——可能永远等不到）时通知用户，每轮连续跳过只通知一次。在运行的不通知：
//!   运行偶尔长过一个周期是常态。下次真正触发、改配置或重新启用时清除；挡路的会话换了（「立即运行」开了新会话）算新的一轮。

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use croner::Cron;
use croner::parser::{CronParser, Seconds};
use muliao_notifications::{Notifications, Notify};
use muliao_platform::db::Tx;
use muliao_platform::ids::SessionId;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::kinds::{Configured, DueRule, TriggerKind, parse_config, stored_time};
use crate::service::{Busy, busy_previous_in};
use crate::store::{Corrupt, Trigger};
use muliao_platform::errors::{Error, Result, invalid};

pub const KIND: &str = "schedule";

#[derive(Debug, Clone, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ScheduleConfig {
    pub cron: String,
}

/// 连续跳过（界面与模型看的部分）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, utoipa::ToSchema)]
pub struct SkippedFires {
    /// 连续跳过了几次
    pub count: u32,
    /// 第一次跳过的时间
    pub since: DateTime<Utc>,
    /// 最近一次跳过时挡路的会话在运行还是在等待
    pub previous_state: Busy,
}

/// 存在 state 里的连续跳过：多记两件不给界面看的事——挡路的是哪个会话（换了就是新的一轮）、这一轮是否已通知过用户。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct SkipStreak {
    #[serde(flatten)]
    shown: SkippedFires,
    session_id: SessionId,
    notified: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct ScheduleState {
    /// 带服务端时区的 RFC 3339
    next_run_at: String,
    skipped: Option<SkipStreak>,
}

/// 界面与模型看到的状态。
#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
pub struct ScheduleStatus {
    pub next_run_at: String,
    /// 正在连续跳过（上一次触发的会话还没做完）时给出，下次真正触发时清除
    pub skipped: Option<SkippedFires>,
}

pub struct Schedule {
    tz: Tz,
    notifications: Notifications,
    description: String,
}

impl Schedule {
    pub fn new(tz: Tz, notifications: Notifications) -> Self {
        let description =
            "Fires on a schedule. config: {\"cron\": \"<5-field cron expression>\"} in the server timezone, \
             e.g. {\"cron\": \"*/5 * * * *\"}. Skips a firing while the conversation the previous firing started is \
             still busy: running, or waiting on background jobs, delegated work, a wake-up or the user's answer; \
             status.skipped then counts the consecutive skips, and the user is notified once when that conversation \
             is waiting. For a task that should happen once, use the `once` kind: a cron expression with a date would \
             fire again every year."
                .to_owned();
        Self { tz, notifications, description }
    }

    /// 表达式在 after 之后的下一次（带服务端时区）。契约是 5 段（分 时 日 月 周）：带秒的 6 段会每秒到期。
    pub fn next_run(&self, expr: &str, after: DateTime<Utc>) -> std::result::Result<DateTime<Tz>, String> {
        let bad = || format!("invalid cron expression {expr:?}: expected 5 fields (minute hour day month weekday)");
        if expr.split_whitespace().count() != 5 {
            return Err(bad());
        }
        let cron: Cron = CronParser::builder()
            .seconds(Seconds::Disallowed)
            .build()
            .parse(expr)
            .map_err(|e| format!("{}: {e}", bad()))?;
        // 语法合法但永远不会到期（如 2 月 30 日）：找不到下一个时间
        cron.find_next_occurrence(&after.with_timezone(&self.tz), false)
            .map_err(|e| format!("cron expression {expr:?} never fires: {e}"))
    }

    fn state(&self, t: &Trigger) -> Result<ScheduleState> {
        t.state_as()
    }

    fn waiting_notice(t: &Trigger, count: u32, session_id: &SessionId) -> Notify {
        Notify {
            title: format!("定时触发器 {} 在跳过", t.name),
            body: format!(
                "上一次触发的会话在等待（后台任务、委派或你的回答），它结束前每次到点都会跳过，已跳过 {count} 次。点开看它在等什么。"
            ),
            // 点开通知打开挡路的那个会话
            session_id: Some(session_id.to_owned()),
            issue_id: None,
        }
    }
}

impl TriggerKind for Schedule {
    fn name(&self) -> &'static str {
        KIND
    }

    fn description(&self) -> &str {
        &self.description
    }

    fn configure(&self, config: &Value, now: DateTime<Utc>) -> Result<Configured> {
        let cron = parse_config::<ScheduleConfig>(KIND, config)?.cron.trim().to_owned();
        let next = self.next_run(&cron, now).map_err(invalid)?;
        // 改配置、重新启用都从头算：之前的连续跳过（连同它是否通知过）不带过来
        let state = ScheduleState { next_run_at: next.to_rfc3339(), skipped: None };
        Ok(Configured {
            config: serde_json::json!({ "cron": cron }),
            state: serde_json::to_value(state).expect("serializes"),
        })
    }

    fn status(&self, t: &Trigger) -> Result<Value> {
        let state = self.state(t)?;
        let status = ScheduleStatus { next_run_at: state.next_run_at, skipped: state.skipped.map(|s| s.shown) };
        Ok(serde_json::to_value(status).expect("serializes"))
    }
}

impl DueRule for Schedule {
    fn kind(&self) -> &'static str {
        KIND
    }

    fn due_at(&self, t: &Trigger) -> Result<DateTime<Utc>> {
        stored_time(t, &self.state(t)?.next_run_at, "next_run_at")
    }

    fn consume_in(&self, tx: &mut Tx<'_>, t: &mut Trigger, now: DateTime<Utc>) -> Result<Option<String>> {
        let mut state = self.state(t)?;
        let cron = t.config_as::<ScheduleConfig>()?.cron;
        // 存下的表达式是配置时校验过的：现在算不出下次时间是存下的数据坏了
        let following = self
            .next_run(&cron, now)
            .map_err(|e| Error::internal(Corrupt(format!("trigger {} ({}): {e}", t.id, t.name))))?;
        state.next_run_at = following.to_rfc3339();
        let Some((session_id, busy)) = busy_previous_in(tx, t)? else {
            state.skipped = None;
            t.set_state(&state);
            return Ok(None);
        };
        // 挡路的会话换了是新的一轮
        let streak = state.skipped.take().filter(|s| s.session_id == session_id);
        let notified = streak.as_ref().is_some_and(|s| s.notified);
        let notify = busy == Busy::Waiting && !notified;
        let count = streak.as_ref().map_or(1, |s| s.shown.count + 1);
        let streak = SkipStreak {
            shown: SkippedFires { count, since: streak.as_ref().map_or(now, |s| s.shown.since), previous_state: busy },
            session_id: session_id.clone(),
            notified: notified || notify,
        };
        if notify {
            self.notifications.notify_in(tx, &Self::waiting_notice(t, count, &session_id))?;
        }
        state.skipped = Some(streak);
        t.set_state(&state);
        Ok(Some(format!("previous run still {} (skipped {count} in a row)", busy.as_str())))
    }
}
