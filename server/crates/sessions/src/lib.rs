//! 会话领域（docs/architecture.md 第 7、8 节）：会话、thread 树、信箱、历史、挂起的调用、会话 actor（运行与每步保存）、
//! 委派、占用与结清、到点唤醒、压缩、停止、删除。

pub mod activity;
pub mod actor;
pub mod answers;
pub mod compaction;
mod content;
pub mod conventions;
mod delegation;
pub mod deletion;
pub mod directory;
pub mod events;
pub mod halt;
pub mod history;
mod holds;
pub mod inbound;
pub mod live;
pub mod mailbox;
pub mod origins;
mod paused_calls;
mod pending;
pub mod recall;
pub mod run;
pub mod runs;
pub mod service;
mod settlement;
mod thread_calls;
pub mod tools;
pub mod transcript;
mod wakeups;
pub mod work;

#[cfg(feature = "testing")]
pub mod testing;
#[cfg(test)]
mod tests;

use muliao_platform::db::Migration;

pub const MIGRATION: Migration = Migration {
    domain: "sessions",
    steps: &[
        include_str!("../migrations/001_sessions.sql"),
        include_str!("../migrations/002_hold_kinds.sql"),
        include_str!("../migrations/003_approval_choice.sql"),
        include_str!("../migrations/004_part_marks.sql"),
        include_str!("../migrations/005_runs_without_kind.sql"),
        include_str!("../migrations/006_shared_files.sql"),
        include_str!("../migrations/007_part_marks_as_array.sql"),
        include_str!("../migrations/008_hold_columns.sql"),
        include_str!("../migrations/009_paused_calls.sql"),
        include_str!("../migrations/010_thread_call_mode.sql"),
    ],
};
