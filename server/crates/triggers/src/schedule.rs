//! 定时触发：config = `{"cron": "<5 段 cron 表达式>"}`，按服务端配置的时区解释。
//!
//! - 下次时间记在 state 的 `next_run_at`；到点先推进到下次（消耗这次到期），再触发（[`crate::due`]）。
//! - 停机期间错过的只补一次，然后从当前时间算下一次；停用后重新启用，下次时间从启用时算起，不补暂停期间的。
//! - 不重叠：上一次触发开出的会话还没做完（[`busy_previous_in`]）时跳过本次；前置检查还在跑、还没有会话时由扫描循环记下的
//!   「在触发」挡住（只消耗不触发）。
//! - 跳过要看得见：连续跳过记在 state 的 `skipped`（次数、从何时起、挡路的会话在运行还是在等待），作为状态给界面与模型看；
//!   挡路的会话在等待（后台任务、委派、用户回答——可能永远等不到）时通知用户，每个挡路会话一次（[`notify_waiting_in`]，
//!   与流式共用）。在运行的不通知：运行偶尔长过一个周期是常态。下次真正触发、改配置或重新启用时清除；挡路的会话换了
//!   （「立即运行」开了新会话）算新的一轮。

use chrono::{DateTime, FixedOffset, Utc};
use chrono_tz::Tz;
use croner::Cron;
use croner::parser::{CronParser, Seconds};
use muliao_notifications::{Notifications, Notify};
use muliao_platform::db::Tx;
use muliao_platform::ids::SessionId;
use serde::{Deserialize, Serialize};

use crate::service::{Busy, busy_previous_in, notify_waiting_in};
use crate::store::Trigger;
use muliao_platform::errors::{Error, Result};

pub const DESCRIPTION: &str = "Fires on a schedule. config: {\"cron\": \"<5-field cron expression>\"} in the server \
     timezone, e.g. {\"cron\": \"*/5 * * * *\"}. Skips a firing while the conversation the previous firing started is \
     still busy: running, or waiting on background jobs, delegated work, a wake-up or the user's answer; \
     status.skipped then counts the consecutive skips, and the user is notified once when that conversation is \
     waiting. For a task that should happen once, use the `once` kind: a cron expression with a date would fire \
     again every year.";

/// 5 段 cron 表达式（分 时 日 月 周），去掉了首尾空白。只经 [`CronExpr::new`] 构造（反序列化也走它）：能解析、
/// 会到期。带秒的 6 段会每秒到期，所以不收。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(into = "String", try_from = "String")]
#[schema(value_type = String)]
pub struct CronExpr(String);

impl CronExpr {
    pub fn new(raw: &str) -> std::result::Result<Self, String> {
        let expr = raw.trim();
        // 语法合法但永远不会到期（如 2 月 30 日）：找不到下一个时间
        Self::parse(expr)?
            .find_next_occurrence(&Utc::now(), false)
            .map_err(|e| format!("cron expression {expr:?} never fires: {e}"))?;
        Ok(Self(expr.to_owned()))
    }

    fn parse(expr: &str) -> std::result::Result<Cron, String> {
        let bad = || format!("invalid cron expression {expr:?}: expected 5 fields (minute hour day month weekday)");
        if expr.split_whitespace().count() != 5 {
            return Err(bad());
        }
        CronParser::builder().seconds(Seconds::Disallowed).build().parse(expr).map_err(|e| format!("{}: {e}", bad()))
    }

    /// after 之后的下一次（带服务端时区）。构造时已确认能解析、会到期，一个到期过的 5 段表达式以后还会到期：这里失败是
    /// 本程序的 bug（内部错误，不 panic：它在写事务里）。
    pub fn next_after(&self, after: DateTime<Utc>, tz: Tz) -> Result<DateTime<FixedOffset>> {
        let next = Self::parse(&self.0)
            .and_then(|cron| cron.find_next_occurrence(&after.with_timezone(&tz), false).map_err(|e| e.to_string()))
            .map_err(|e| Error::internal(format!("cron expression {:?} has no next time: {e}", self.0)))?;
        Ok(next.fixed_offset())
    }
}

impl TryFrom<String> for CronExpr {
    type Error = String;

    fn try_from(raw: String) -> std::result::Result<Self, String> {
        Self::new(&raw)
    }
}

impl From<CronExpr> for String {
    fn from(c: CronExpr) -> String {
        c.0
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ScheduleConfig {
    pub cron: CronExpr,
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

/// 存在 state 里的连续跳过：多记挡路的是哪个会话（换了就是新的一轮）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SkipStreak {
    #[serde(flatten)]
    shown: SkippedFires,
    session_id: SessionId,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScheduleState {
    /// 带服务端时区
    pub next_run_at: DateTime<FixedOffset>,
    pub skipped: Option<SkipStreak>,
}

/// 界面与模型看到的状态。
#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
pub struct ScheduleStatus {
    #[schema(value_type = String)]
    pub next_run_at: DateTime<FixedOffset>,
    /// 正在连续跳过（上一次触发的会话还没做完）时给出，下次真正触发时清除
    pub skipped: Option<SkippedFires>,
}

/// 创建、改配置、重新启用：下次时间从 now 算起，之前的连续跳过不带过来。
pub fn initial(config: &ScheduleConfig, tz: Tz, now: DateTime<Utc>) -> Result<ScheduleState> {
    Ok(ScheduleState { next_run_at: config.cron.next_after(now, tz)?, skipped: None })
}

pub fn status(state: &ScheduleState) -> ScheduleStatus {
    ScheduleStatus { next_run_at: state.next_run_at, skipped: state.skipped.as_ref().map(|s| s.shown.clone()) }
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

/// 在到期的事务里消耗这次到期：推进到下次；上一次的会话还没做完时记一次跳过（返回原因，这次不触发）。调用方把 state
/// 放回这一行并写回。
pub fn consume_in(
    tx: &mut Tx<'_>,
    t: &mut Trigger,
    config: &ScheduleConfig,
    state: &mut ScheduleState,
    now: DateTime<Utc>,
    tz: Tz,
    notifications: &Notifications,
) -> Result<Option<String>> {
    state.next_run_at = config.cron.next_after(now, tz)?;
    let Some((session_id, busy)) = busy_previous_in(tx, t)? else {
        state.skipped = None;
        return Ok(None);
    };
    // 挡路的会话换了是新的一轮
    let streak = state.skipped.take().filter(|s| s.session_id == session_id);
    let count = streak.as_ref().map_or(1, |s| s.shown.count + 1);
    let since = streak.as_ref().map_or(now, |s| s.shown.since);
    state.skipped =
        Some(SkipStreak { shown: SkippedFires { count, since, previous_state: busy }, session_id: session_id.clone() });
    notify_waiting_in(tx, notifications, t, &session_id, busy, |t| waiting_notice(t, count, &session_id))?;
    Ok(Some(format!("previous run still {busy} (skipped {count} in a row)")))
}
