//! 触发器（docs/architecture.md 第 14 节）：具名、持久的事件来源，把外部刺激变成投进会话信箱的信——定时（schedule）、一次性
//! （once）、流式（stream），前置检查，脚本出错告警。
//!
//! - [`kinds`]：触发方式（[`kinds::Kind`]、存下的 [`kinds::Mode`]）。种类都在本 crate，是 enum：加一种 = 各 enum 加一个变体，
//!   编译器列出要跟着补的地方，表与工具不用改。
//! - [`service`]：管理与触发的唯一出口；[`precheck`]：前置检查；[`faults`]：脚本出错的记录与告警。
//! - 激活机制：[`due::DueRunner`]（定时与一次性共用的到期扫描）、[`stream::StreamRunner`]（托管常驻命令）。
//!
//! 组合根：[`TriggerDomain::build`] → [`TriggerDomain::tools`] 进工具箱 → [`TriggerDomain::origin_note`] 合进 `OriginNotes` →
//! `service.deletion_hook()` 交给 agent 的删除流程 → 对外服务时 [`TriggerDomain::start`] → 关停时 [`Activation::shutdown`]。

pub mod due;
pub mod faults;
pub mod kinds;
pub mod once;
pub mod precheck;
pub mod schedule;
pub mod service;
pub mod store;
pub mod stream;
pub mod tools;

#[cfg(test)]
mod tests;

use std::sync::Arc;
use std::time::Duration;

use chrono_tz::Tz;
use muliao_notifications::Notifications;
use muliao_platform::db::{Database, Migration};
use muliao_platform::tooling::BuiltinTool;
use muliao_sessions::SenderInbox;
use muliao_workspace::commands::AgentCommands;
use tokio::task::JoinHandle;

pub use service::{Fired, NewSpec, PrecheckChange, Prompt, TriggerName, TriggerService, TriggerView, Update};

pub const MIGRATION: Migration = Migration {
    domain: "triggers",
    steps: &[include_str!("../migrations/001_triggers.sql"), include_str!("../migrations/002_typed_modes.sql")],
};

/// 触发器领域要的外部依赖。
pub struct TriggerDeps {
    pub db: Arc<Database>,
    pub tz: Tz,
    pub inbox: SenderInbox,
    pub notifications: Notifications,
    pub commands: Arc<AgentCommands>,
    /// runtime.precheck_timeout_seconds
    pub precheck_timeout_seconds: f64,
}

pub struct TriggerDomain {
    pub service: TriggerService,
    pub due: Arc<due::DueRunner>,
    pub streams: Arc<stream::StreamRunner>,
    tools: Vec<BuiltinTool>,
}

/// 起来了的激活机制：两个常驻循环与托管流式命令的那一个。只有它能关停，关停时被消耗。
#[must_use = "dropping it leaves the trigger loops running with no way to shut them down"]
pub struct Activation {
    loops: [JoinHandle<()>; 2],
    streams: Arc<stream::StreamRunner>,
}

impl Activation {
    /// 关停：停掉常驻循环（不再起新的），再杀掉流式命令（它们自成进程组，不随服务退出）。正在跑的触发随进程结束
    /// （至多一次）。
    pub async fn shutdown(self) {
        for handle in self.loops {
            handle.abort();
        }
        self.streams.shutdown().await;
    }
}

impl TriggerDomain {
    pub fn build(d: TriggerDeps) -> Self {
        Self::build_with(d, stream::Timing::default())
    }

    fn build_with(d: TriggerDeps, timing: stream::Timing) -> Self {
        let faults = faults::Faults::new(d.inbox.clone(), d.notifications.clone(), d.tz);
        let hosting = stream::Hosting::default();
        let service = TriggerService::new(
            d.db,
            d.tz,
            d.commands.clone(),
            Duration::from_secs_f64(d.precheck_timeout_seconds),
            d.inbox,
            faults,
            hosting.clone(),
        );
        let due = Arc::new(due::DueRunner::new(service.clone(), d.tz, d.notifications.clone()));
        let streams =
            Arc::new(stream::StreamRunner::new(service.clone(), d.commands, d.notifications, timing, hosting));
        let tools = tools::tools(service.clone(), d.precheck_timeout_seconds);
        Self { service, due, streams, tools }
    }

    pub fn tools(&self) -> Vec<BuiltinTool> {
        self.tools.clone()
    }

    /// 触发器开出的会话在指令里的说明。
    pub fn origin_note() -> &'static str {
        service::SESSION_NOTE
    }

    /// 起激活机制（到期扫描、流式托管）。对外服务时调用一次：生产里由组合根的 `App::start` 消耗 App 保证只起一次。
    pub fn start(&self) -> Activation {
        Activation { loops: [self.due.clone().run(), self.streams.clone().run()], streams: self.streams.clone() }
    }
}
