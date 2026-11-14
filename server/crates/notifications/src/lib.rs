//! 通知领域（docs/architecture.md 第 14 节）：通知（收件箱、未读、会话未读的派生）、推送订阅与 Web Push、notify_user 工具。
//!
//! - [`inbox`]：`notifications` 表，通知从哪来、谁读；别的领域发通知只经 [`inbox::Notifications::notify_in`]（同层的
//!   issue 由组合根注入成函数）。
//! - [`push`]：`push_subscriptions` 表，通知提交后推到每台设备、暂时失败在内存里退避重试。
//! - [`webpush`]：Web Push 协议（加密、VAPID 签名、回应分类）。
//!
//! 组合根：`VapidKeys::load_or_create(settings.vapid_key_path())` → `Push::new` → `Notifications::new`；
//! `Notifications::subscribe` 登记进会话事件的订阅者；`tools::tools` 进工具箱。

pub mod config;
pub mod inbox;
pub mod push;
pub mod tools;
pub mod webpush;

#[cfg(test)]
mod tests;

use muliao_platform::db::Migration;

pub use inbox::{Notification, Notifications, Notify};
pub use push::Push;
pub use webpush::{Subscription, VapidKeys};

pub const MIGRATION: Migration =
    Migration { domain: "notifications", steps: &[include_str!("../migrations/001_notifications.sql")] };
