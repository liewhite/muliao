//! 收件箱：发给用户的通知（`notifications` 表只在这里读写）。通知页与应用角标的未读数是它的展示，推到设备是
//! [`crate::push`] 的事：写下通知的事务提交后交给它。
//!
//! 通知从哪来（docs/architecture.md 第 14 节）：运行结束时用户在等的回答或提问、等批准（订阅 `RunEnded`，规则只在
//! [`worth_pushing`]）；运行最终失败（`RunFailed`）；agent 主动通知（`notify_user`）；别的领域在自己事务里发的（[`Notifications::notify_in`]：
//! 触发器告警投不到 agent 时的兜底、定时被挡、issue 交给用户）；开启推送时的测试。
//!
//! 会话的未读不另存：有指向它的未读通知就是未读（[`unread_sessions_in`]），所以正常跑完不通知的定时会话不算未读。

use std::collections::BTreeSet;
use std::sync::Arc;

use chrono::{DateTime, Utc};
use muliao_platform::bus::{Bus, ServerEvent};
use muliao_platform::db::{Database, Tx};
use muliao_platform::text::truncate;
use muliao_sessions::answers::Waiting;
use muliao_sessions::events::{BoxError, RunEnded, RunFailed, SessionDeleted, Subscribers};
use rusqlite::{Connection, OptionalExtension, Row, params, params_from_iter};
use serde::Serialize;

use crate::push::Push;

/// 运行失败的通知正文（错误原文）最多这么多字符，全文在会话页
const FAILURE_BODY_MAX: usize = 500;

/// 给用户发一条通知的请求（别的领域经组合根注入的端口调 [`Notifications::notify_in`]）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notify {
    pub title: String,
    pub body: String,
    /// 通知关于的会话（打开它即已读、随它删除）；系统通知为 None
    pub session_id: Option<String>,
    /// 交给用户审核的 issue：点开打开它（而不是会话），可以直接通过或打回；其余为 None
    pub issue_id: Option<i64>,
}

/// 一条通知（读模型）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Notification {
    pub id: i64,
    pub title: String,
    pub body: String,
    pub session_id: Option<String>,
    pub issue_id: Option<i64>,
    /// 点开打开的页面（推送与通知页共用）；系统通知为 None
    pub url: Option<String>,
    pub unread: bool,
    pub created_at: DateTime<Utc>,
}

/// 点开通知打开的页面：交给用户审核的 issue，否则通知关于的会话；都没有时为 None（推送点开看通知页）。
fn url_of(session_id: Option<&str>, issue_id: Option<i64>) -> Option<String> {
    match (issue_id, session_id) {
        (Some(issue), _) => Some(format!("/issues/{issue}")),
        (None, Some(session)) => Some(format!("/sessions/{session}")),
        (None, None) => None,
    }
}

const COLUMNS: &str = "id, title, body, session_id, issue_id, unread, created_at";

fn notification(r: &Row<'_>) -> rusqlite::Result<Notification> {
    let session_id: Option<String> = r.get(3)?;
    let issue_id: Option<i64> = r.get(4)?;
    Ok(Notification {
        id: r.get(0)?,
        title: r.get(1)?,
        body: r.get(2)?,
        url: url_of(session_id.as_deref(), issue_id),
        session_id,
        issue_id,
        unread: r.get(5)?,
        created_at: r.get(6)?,
    })
}

/// 推送一次运行的回答吗（推送规则只在这里）。停下来等用户批准的总是推：没人在看的运行（触发器、issue）也只有用户能批。
/// 其余的要处理过的信件里有用户在等的、且有话要告诉用户：做完了，或停下来问用户；用户自己停止的、只在等别的 agent 的不推。
pub fn worth_pushing(e: &RunEnded) -> bool {
    if e.waiting_on.contains(&Waiting::Approval) {
        return true;
    }
    e.awaited && !e.stopped && (e.waiting_on.is_empty() || e.waiting_on.contains(&Waiting::AskUser))
}

/// 推到设备上的一条通知（推送时从收件箱取）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PushMessage {
    pub notification_id: i64,
    pub title: String,
    pub body: String,
    pub url: Option<String>,
    pub session_id: Option<String>,
    pub issue_id: Option<i64>,
    /// 此刻的未读通知数（应用角标）
    pub unread: i64,
}

/// 推送时的内容：通知本身与此刻的未读数；通知已被删除时为 None。
pub(crate) fn push_message_in(c: &Connection, notification_id: i64) -> rusqlite::Result<Option<PushMessage>> {
    let Some(n) = c
        .query_row(&format!("SELECT {COLUMNS} FROM notifications WHERE id = ?1"), [notification_id], notification)
        .optional()?
    else {
        return Ok(None);
    };
    Ok(Some(PushMessage {
        notification_id: n.id,
        title: n.title,
        body: n.body,
        url: n.url,
        session_id: n.session_id,
        issue_id: n.issue_id,
        unread: unread_count_in(c)?,
    }))
}

pub fn unread_count_in(c: &Connection) -> rusqlite::Result<i64> {
    c.query_row("SELECT count(*) FROM notifications WHERE unread", [], |r| r.get(0))
}

/// 这些会话里未读的：有指向它的未读通知（会话列表、会话页用）。
pub fn unread_sessions_in(c: &Connection, session_ids: &[String]) -> rusqlite::Result<BTreeSet<String>> {
    // 调用方一次传一页会话列表，远小于 SQLite 一条语句的参数上限（32766）
    let marks = vec!["?"; session_ids.len()].join(", ");
    let mut stmt =
        c.prepare(&format!("SELECT DISTINCT session_id FROM notifications WHERE unread AND session_id IN ({marks})"))?;
    stmt.query_map(params_from_iter(session_ids), |r| r.get::<_, String>(0))?.collect()
}

/// 通知的写入与查询。克隆共享同一份。
#[derive(Clone)]
pub struct Notifications {
    db: Arc<Database>,
    bus: Arc<Bus>,
    push: Arc<Push>,
}

impl Notifications {
    pub fn new(db: Arc<Database>, bus: Arc<Bus>, push: Arc<Push>) -> Self {
        Self { db, bus, push }
    }

    pub fn push(&self) -> &Push {
        &self.push
    }

    /// 在调用方的事务里写下一条通知；提交后推给前端（SSE `notification`）并推到每台设备。返回通知的 id。
    pub fn notify_in(&self, tx: &mut Tx<'_>, n: &Notify) -> rusqlite::Result<i64> {
        tx.execute(
            "INSERT INTO notifications (title, body, session_id, issue_id, unread, created_at) VALUES (?1, ?2, ?3, ?4, 1, ?5)",
            params![n.title, n.body, n.session_id, n.issue_id, Utc::now()],
        )?;
        let id = tx.last_insert_rowid();
        let (bus, push, session_id) = (self.bus.clone(), self.push.clone(), n.session_id.clone());
        tx.on_commit(move || {
            // 前端据此刷新通知页与未读数；正看着这个会话时把它标为已读
            bus.publish(ServerEvent {
                kind: "notification",
                data: serde_json::json!({"notification_id": id, "session_id": session_id}),
            });
            push.deliver(id);
        });
        Ok(id)
    }

    /// 自己开事务发一条通知。
    pub async fn notify(&self, n: Notify) -> rusqlite::Result<i64> {
        let this = self.clone();
        self.db.write(move |tx| this.notify_in(tx, &n)).await
    }

    /// 用户在界面上开启推送后发的测试通知（系统通知，不打开会话）。
    pub async fn send_test(&self) -> rusqlite::Result<i64> {
        self.notify(Notify {
            title: "Muliao".into(),
            body: "推送通知已开启。".into(),
            session_id: None,
            issue_id: None,
        })
        .await
    }

    /// 登记对会话事件的订阅：运行结束、最终失败时通知，会话删除时删掉指向它的通知。
    pub fn subscribe(&self, subscribers: &mut Subscribers) {
        let this = self.clone();
        subscribers.on_run_ended(move |tx, e| this.on_run_ended(tx, e));
        let this = self.clone();
        subscribers.on_run_failed(move |tx, e| this.on_run_failed(tx, e));
        subscribers.on_session_deleted(on_session_deleted);
    }

    fn on_run_ended(&self, tx: &mut Tx<'_>, e: &RunEnded) -> Result<(), BoxError> {
        if !worth_pushing(e) {
            return Ok(());
        }
        let n = Notify {
            title: format!("{} · {}", e.agent_name, e.session_title),
            body: e.answer.clone(),
            session_id: Some(e.session_id.clone()),
            issue_id: None,
        };
        self.notify_in(tx, &n)?;
        Ok(())
    }

    /// 运行最终失败：通知用户，点开是那个会话；运行中途的工具报错不走这里。
    fn on_run_failed(&self, tx: &mut Tx<'_>, e: &RunFailed) -> Result<(), BoxError> {
        let session = muliao_sessions::directory::get_in(tx, &e.session_id)?
            .ok_or_else(|| format!("run {} failed in session {}, which does not exist", e.run_id, e.session_id))?;
        let n = Notify {
            title: format!("{} · {} 运行失败", e.agent_name, session.title),
            body: truncate(e.error.trim(), FAILURE_BODY_MAX),
            session_id: Some(e.session_id.clone()),
            issue_id: None,
        };
        self.notify_in(tx, &n)?;
        Ok(())
    }

    /// 最新的 limit 条，新的在前。
    pub async fn list(&self, limit: usize) -> rusqlite::Result<Vec<Notification>> {
        self.db
            .read(move |c| {
                let mut stmt =
                    c.prepare_cached(&format!("SELECT {COLUMNS} FROM notifications ORDER BY id DESC LIMIT ?1"))?;
                stmt.query_map([limit as i64], notification)?.collect()
            })
            .await
    }

    pub async fn unread_count(&self) -> rusqlite::Result<i64> {
        self.db.read(unread_count_in).await
    }

    pub async fn unread_sessions(&self, session_ids: Vec<String>) -> rusqlite::Result<BTreeSet<String>> {
        self.db.read(move |c| unread_sessions_in(c, &session_ids)).await
    }

    /// 通知页：标记到 through_id 为止的通知已读，之后才到的用户还没看到，保持未读。
    pub async fn mark_read(&self, through_id: i64) -> rusqlite::Result<()> {
        self.mark("id <= ?1", through_id.into()).await
    }

    /// 用户看过了会话（打开它、看着它时又来了通知）：指向它的通知已读（会话的未读就是这些通知）。
    pub async fn mark_session_read(&self, session_id: &str) -> rusqlite::Result<()> {
        self.mark("session_id = ?1", session_id.to_owned().into()).await
    }

    /// 用户打开了 issue：请他审核它的通知已读。
    pub async fn mark_issue_read(&self, issue_id: i64) -> rusqlite::Result<()> {
        self.mark("issue_id = ?1", issue_id.into()).await
    }

    async fn mark(&self, filter: &'static str, value: rusqlite::types::Value) -> rusqlite::Result<()> {
        self.db
            .write(move |tx| {
                tx.execute(&format!("UPDATE notifications SET unread = 0 WHERE unread AND {filter}"), [value]).map(drop)
            })
            .await
    }
}

/// 指向被删会话的通知随它删除。
fn on_session_deleted(tx: &mut Tx<'_>, e: &SessionDeleted) -> Result<(), BoxError> {
    tx.execute("DELETE FROM notifications WHERE session_id = ?1", [&e.session_id])?;
    Ok(())
}
