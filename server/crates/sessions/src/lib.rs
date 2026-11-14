//! 会话领域（docs/architecture.md 第 7、8 节）：会话、thread 树、信箱、历史、挂起的调用、会话 actor（运行与每步保存）、
//! 委派、占用与结清、到点唤醒、压缩、停止、删除。

pub mod activity;
pub mod deletion;
pub mod events;
pub mod execution;
pub mod intake;
pub mod mailbox;
pub mod outside;
pub mod recall;
pub mod record;
pub mod service;
mod thread_calls;
pub mod tools;
pub mod waiting;

#[cfg(feature = "testing")]
pub mod testing;
#[cfg(test)]
mod tests;

use muliao_platform::db::Migration;
use muliao_platform::ids::SessionId;
use tokio::sync::mpsc;

use crate::execution::actor::{Actors, StopRequest};
use crate::execution::run::{Core, SessionDeps};
use crate::service::SessionService;

/// 建会话领域：起会话 actor 的分发，返回它们与界面门面（共用一份 [`Core`]）。之后调 [`Actors::resume`] 接着跑重启前
/// 没做完的。wakes、stops：[`crate::execution::actor::Waker::channel`] 与 [`crate::execution::actor::Stopper::channel`] 的接收端。
pub fn start(
    deps: SessionDeps,
    wakes: mpsc::UnboundedReceiver<SessionId>,
    stops: mpsc::UnboundedReceiver<StopRequest>,
) -> (Actors, SessionService) {
    let core = std::sync::Arc::new(Core::new(deps));
    let actors = Actors::start(core.clone(), wakes, stops);
    (actors.clone(), SessionService::new(core, actors))
}

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
        include_str!("../migrations/011_file_refs.sql"),
    ],
};
