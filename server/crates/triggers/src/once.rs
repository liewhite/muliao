//! 一次性触发：config = `{"at": "<ISO 8601 时间>"}`，不带时区的按服务端配置的时区解释。
//!
//! 到点触发一次，这一次用掉之后就结束（finished_at）：不再出现在默认列表，也不能再修改、启用或立即运行，但行保留着，它
//! 发起过的会话仍查得到是谁发的。「用掉」与触发的结果无关——真的投递、前置检查判定没事、触发失败都算用掉；结束与触发的
//! 先后由 [`crate::due`] 保证（至多一次）。「立即运行」不消耗它预定的那一次：那是管理操作，用来试跑。
//!
//! 停用期间错过了预定时间的，重新启用时 configure 会拒绝（时间已过去）：要么改成新的时间，要么删掉——不补一次，也不悄悄顺延。

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use muliao_platform::db::Tx;
use muliao_platform::time::parse_local;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::kinds::{Configured, DueRule, TriggerKind, parse_config, stored_time};
use crate::store::Trigger;
use muliao_platform::errors::{Result, invalid};

pub const KIND: &str = "once";

#[derive(Debug, Clone, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct OnceConfig {
    pub at: String,
}

#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
pub struct OnceStatus {
    pub run_at: String,
}

pub struct Once {
    tz: Tz,
}

impl Once {
    pub fn new(tz: Tz) -> Self {
        Self { tz }
    }

    fn at(&self, t: &Trigger) -> Result<DateTime<Utc>> {
        stored_time(t, &t.config_as::<OnceConfig>()?.at, "time")
    }
}

impl TriggerKind for Once {
    fn name(&self) -> &'static str {
        KIND
    }

    fn description(&self) -> &str {
        "Fires once at a given time and then finishes: config {\"at\": \"<ISO 8601 time>\"}, e.g. \
         {\"at\": \"2026-10-22T10:30\"}; without an offset it is read in the server timezone, and it must be in the \
         future. Use this for anything that should happen once — a cron expression with a date would fire again every \
         year and stay in the trigger list. It starts a new conversation: to continue the current one later (e.g. an \
         issue you are working on), use `wake_me` instead. Finished triggers leave the list; the conversations they \
         started stay and can be listed with trigger_list(finished=true)."
    }

    fn configure(&self, config: &Value, now: DateTime<Utc>) -> Result<Configured> {
        let text = parse_config::<OnceConfig>(KIND, config)?.at;
        let at = parse_local(&text, self.tz).ok_or_else(|| {
            invalid(format!("once trigger time {text:?} is not an ISO 8601 time string, e.g. \"2026-10-22T10:30\""))
        })?;
        if at <= now {
            return Err(invalid(format!("once trigger time {text:?} is in the past")));
        }
        // 到期时间就是配置本身，没有随运行变化的状态
        Ok(Configured {
            config: serde_json::json!({ "at": at.with_timezone(&self.tz).to_rfc3339() }),
            state: serde_json::json!({}),
        })
    }

    fn status(&self, t: &Trigger) -> Result<Value> {
        let run_at = self.at(t)?.with_timezone(&self.tz).to_rfc3339();
        Ok(serde_json::to_value(OnceStatus { run_at }).expect("serializes"))
    }
}

impl DueRule for Once {
    fn kind(&self) -> &'static str {
        KIND
    }

    fn due_at(&self, t: &Trigger) -> Result<DateTime<Utc>> {
        self.at(t)
    }

    /// 到点让它结束，这一次就算用掉了。
    fn consume_in(&self, _tx: &mut Tx<'_>, t: &mut Trigger, now: DateTime<Utc>) -> Result<Option<String>> {
        t.finished_at = Some(now);
        tracing::info!(trigger = %t.name, "once trigger finished");
        Ok(None)
    }
}
