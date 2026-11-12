//! 会话领域（docs/architecture.md 第 7、8 节）：会话、thread 树、信箱、历史、挂起的调用、会话 actor（运行与每步保存）、
//! 委派、占用与结清、到点唤醒、压缩、停止、删除。

pub mod activity;
pub mod actor;
pub mod answers;
pub mod approvals;
pub mod conventions;
pub mod delegation;
pub mod directory;
pub mod error;
pub mod history;
pub mod holds;
pub mod inbound;
pub mod mailbox;
pub mod origins;
pub mod pending;
pub mod runs;
pub mod settlement;
pub mod thread_calls;
pub mod wakeups;
pub mod work;

use muliao_platform::db::Migration;

pub const MIGRATION: Migration =
    Migration { domain: "sessions", steps: &[include_str!("../migrations/001_sessions.sql")] };
