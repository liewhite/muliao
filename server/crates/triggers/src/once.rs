//! 一次性触发：config = `{"at": "<ISO 8601 时间>"}`，不带时区的按服务端配置的时区解释，存成带时区的时间。
//!
//! 到点触发一次，这一次用掉之后就结束（finished_at）：不再出现在默认列表，也不能再修改、启用或立即运行，但行保留着，它
//! 发起过的会话仍查得到是谁发的。「用掉」与触发的结果无关——真的投递、前置检查判定没事、触发失败都算用掉；结束与触发的
//! 先后由 [`crate::due`] 保证（至多一次）。「立即运行」不消耗它预定的那一次：那是管理操作，用来试跑。
//!
//! 停用期间错过了预定时间的，重新启用时 configure 会拒绝（时间已过去）：要么改成新的时间，要么删掉——不补一次，也不悄悄顺延。

use chrono::{DateTime, FixedOffset, Utc};
use chrono_tz::Tz;
use muliao_platform::time::parse_local;
use serde::{Deserialize, Serialize};

use muliao_platform::errors::{Result, invalid};

pub const DESCRIPTION: &str = "Fires once at a given time and then finishes: config {\"at\": \"<ISO 8601 time>\"}, \
     e.g. {\"at\": \"2026-10-22T10:30\"}; without an offset it is read in the server timezone, and it must be in the \
     future. Use this for anything that should happen once — a cron expression with a date would fire again every \
     year and stay in the trigger list. It starts a new conversation: to continue the current one later (e.g. an \
     issue you are working on), use `wake_me` instead. Finished triggers leave the list; the conversations they \
     started stay and can be listed with trigger_list(finished=true).";

/// 交进来的配置：时间可以不带时区。
#[derive(Debug, Clone, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct OnceInput {
    pub at: String,
}

/// 存下的配置：带服务端时区的到期时间。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct OnceConfig {
    #[schema(value_type = String)]
    pub at: DateTime<FixedOffset>,
}

impl OnceConfig {
    pub fn input(&self) -> OnceInput {
        OnceInput { at: self.at.to_rfc3339() }
    }
}

#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
pub struct OnceStatus {
    #[schema(value_type = String)]
    pub run_at: DateTime<FixedOffset>,
}

/// 解析时间（必须在将来），换成服务端时区。
pub fn configure(input: &OnceInput, tz: Tz, now: DateTime<Utc>) -> Result<OnceConfig> {
    let text = &input.at;
    let at = parse_local(text, tz).ok_or_else(|| {
        invalid(format!("once trigger time {text:?} is not an ISO 8601 time string, e.g. \"2026-10-22T10:30\""))
    })?;
    if at <= now {
        return Err(invalid(format!("once trigger time {text:?} is in the past")));
    }
    Ok(OnceConfig { at: at.with_timezone(&tz).fixed_offset() })
}

pub fn status(config: &OnceConfig) -> OnceStatus {
    OnceStatus { run_at: config.at }
}
