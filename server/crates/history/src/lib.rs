//! 工作历史与回看（docs/architecture.md 第 22 节）：
//!
//! - 冷历史（[`log`]）：agent 自己写的工作历史，一件事一行，只追加（库里的触发器拒绝改删），按时间范围、重要程度与
//!   关键词检索；不随 agent 删除而消失，所以这个领域不参与删除 agent 的清理。
//! - 热历史：还没总结的活动直接从会话领域的读模型（`muliao_sessions::recall`）与 issue 看板读。
//! - 工具（[`tools`]）：history_append、history_search、activity_overview、session_read。

pub mod error;
pub mod log;
pub mod tools;

#[cfg(test)]
mod tests;

use muliao_platform::db::Migration;

pub use error::{Error, Result};
pub use log::{History, NewEntry, Record, Search};
pub use tools::tools;

pub const MIGRATION: Migration =
    Migration { domain: "history", steps: &[include_str!("../migrations/001_history.sql")] };
