//! issue 看板（docs/architecture.md 第 21 节）：状态、优先级、owner 与指派人、无环依赖、只增不改的时间线、投进每个
//! (issue, agent) 唯一会话的投递、停滞与运行结束表态、审核通过与打回、issue_stop、删除 agent 时置空。
//!
//! - [`model`]：issue 本身（`issues` 表）与取值；[`activity`]：时间线（`issue_activities` 表）；[`graph`]：依赖图
//!   （`issue_dependencies` 表）。
//! - [`rules`]：纯规则（投给谁、停滞、能不能审核 / 开工 / 叫停）；[`render`]：给模型看的文本。
//! - [`change`]：变更单元，一次修改的全部后果（记动态、投递、通知、放行下游）只在这里结算。
//! - [`board`]：门面（查询、命令、对会话事件与删除 agent 的反应）；[`tools`]：issue_* 工具。
//!
//! 组合根：`Board::new(db, SenderInbox, notifications, stopper, tz)`；`board.subscribe` 登记进会话事件的订阅者；`board.deletion_hook()` 交给 `AgentLifecycle`；`tools::tools` 进
//! 工具箱；`(Origin::Issue, rules::SESSION_NOTE)` 交给 `OriginNotes`。交给用户的通知点开是 issue 页（[`route`]），
//! 界面打开 issue 页时按这个地址把通知标已读。

pub mod activity;
pub mod board;
pub mod change;
mod conversation;
pub mod graph;
pub mod model;
pub mod render;
pub mod rules;
pub mod tools;

#[cfg(test)]
mod tests;

use muliao_platform::db::Migration;
use muliao_platform::ids::IssueId;

pub use board::{
    Board, BoardIssue, Filter, IssueDetail, IssueWork, ListedIssue, NewIssue, Stopping, UserActions, user_actions_in,
};
pub use change::Patch;
pub use model::{Actor, Assignee, By, Issue, Priority, Status};

/// issue 页（前端路由，与 web/src/routes.ts 的 issueRoute 一致，由 crates/muliao/tests/frontend_contract.rs 核对）：交给
/// 用户的通知点开打开它。
pub fn route(issue_id: IssueId) -> String {
    format!("/issues/{issue_id}")
}

pub const MIGRATION: Migration = Migration {
    domain: "issues",
    steps: &[include_str!("../migrations/001_issues.sql"), include_str!("../migrations/002_tagged_assignee.sql")],
};
