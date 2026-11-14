//! 会话领域（docs/architecture.md 第 7、8 节）：会话、thread 树、信箱、历史、挂起的调用、会话 actor（运行与每步保存）、
//! 委派、占用与结清、到点唤醒、压缩、停止、删除。

pub mod activity;
pub mod deletion;
pub mod directory;
pub mod events;
pub(crate) mod execution;
pub(crate) mod intake;
pub mod mailbox;
pub(crate) mod outside;
pub mod recall;
pub(crate) mod record;
pub mod service;
mod thread_calls;
pub mod tools;
pub(crate) mod waiting;

#[cfg(feature = "testing")]
pub mod testing;
#[cfg(test)]
mod tests;

// 子领域是内部分组：别的领域用到的接口从这里导出，内部怎么分不影响它们
pub use crate::execution::actor::{Actors, Waker};
pub use crate::execution::halt::Halted;
pub use crate::execution::live::{LiveSegment, Retrying, RunEventEnvelope, SavedEvent};
pub use crate::execution::runs::{Outcome, RunRecord};
pub use crate::intake::inbound::{Refusal, Refused, SenderInbox, SenderMail, SenderTarget, UserMessage, UserTarget};
pub use crate::intake::origins::{Origin, OriginNotes, Sender, SessionOrigin, ThreadName};
pub use crate::outside::work::{CheckBeats, SessionWork, TICK, no_progress_hint};
pub use crate::record::compaction::CompactionPolicy;
pub use crate::record::transcript::{Item, SavedItem};
pub use crate::waiting::answers::{CallResult, Choice, QuestionAnswer, Waiting};

use muliao_platform::db::Migration;
use muliao_platform::ids::SessionId;
use tokio::sync::mpsc;

use std::sync::Arc;

use muliao_assets::service::AgentService;
use muliao_files::FileStore;
use muliao_platform::bus::Bus;
use muliao_platform::db::Database;
use muliao_runtime::Runtime;
use muliao_runtime::toolbox::Redaction;

use crate::events::Events;
use crate::execution::actor::StopRequest;
use crate::execution::live::Live;
use crate::record::transcript::Renders;
use crate::service::SessionService;

/// 建会话领域：起会话 actor 的分发，返回它们与界面门面（共用一份 [`Core`]）。之后调 [`Actors::resume`] 接着跑重启前
/// 没做完的。wakes、stops：[`crate::execution::actor::Waker::channel`] 与 [`crate::execution::actor::Stopper::channel`] 的接收端。
pub fn start(
    deps: SessionDeps,
    wakes: mpsc::UnboundedReceiver<SessionId>,
    stops: mpsc::UnboundedReceiver<StopRequest>,
) -> (Actors, SessionService) {
    let core = Arc::new(Core::new(deps));
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

/// 会话领域要别的领域给的部件，组合根交一次（[`crate::start`]）。
pub struct SessionDeps {
    pub db: Arc<Database>,
    pub runtime: Arc<Runtime>,
    pub files: Arc<FileStore>,
    pub agents: Arc<AgentService>,
    pub events: Arc<Events>,
    pub notes: OriginNotes,
    pub waker: Waker,
    pub compaction: CompactionPolicy,
    pub renders: Renders,
    pub redaction: Redaction,
    pub tz: chrono_tz::Tz,
    pub bus: Arc<Bus>,
}

/// 会话领域各处共用的部件：交进来的依赖，加上由它建出的实时状态。会话 actor 与界面门面共用一份。
pub(crate) struct Core {
    pub(crate) d: SessionDeps,
    pub(crate) live: Live,
}

impl Core {
    pub(crate) fn new(deps: SessionDeps) -> Self {
        Self { live: Live::new(deps.bus.clone()), d: deps }
    }
}
