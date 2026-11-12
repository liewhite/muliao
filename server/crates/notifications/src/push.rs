//! 推送：把收件箱（[`crate::inbox`]）里的通知推到用户的设备。设备是浏览器推送订阅（`push_subscriptions` 表只在这里读写）。
//!
//! 写下通知的事务提交后（`Tx::on_commit`）交给 [`Push::deliver`]：每台设备一个独立的 tokio 任务，暂时失败只重试这一台，
//! 按指数退避，从第一次失败起约 30 分钟（本机到推送服务的网络常断十几分钟，#208）。重试只在内存里（#675 已认可）：服务
//! 恰好在重试期间重启，这条推送就不发了，通知本身和未读角标都还在。每次尝试前重读通知与订阅：通知随会话删了、设备退订了
//! 或已失效被删了就不再推；角标取发出那一刻的未读数。

use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use muliao_platform::db::Database;
use muliao_platform::text::truncate;
use rusqlite::{Connection, OptionalExtension, params};
use serde::Serialize;
use tokio::time::Instant;

use crate::inbox::{PushMessage, push_message_in};
use crate::webpush::{self, Device, InvalidDevice, Sent, VapidKeys};

/// 推送服务限制载荷约 4KB（加密后），标题、正文截断留足余量（中文一个字三字节）
const TITLE_MAX_CHARS: usize = 120;
const BODY_MAX_CHARS: usize = 600;

/// 暂时失败的重试：间隔从 first 起翻倍到 max，从第一次失败起超过 window 就放弃。
#[derive(Debug, Clone, Copy)]
pub struct Retry {
    pub first: Duration,
    pub max: Duration,
    pub window: Duration,
}

impl Default for Retry {
    fn default() -> Self {
        Self { first: Duration::from_secs(5), max: Duration::from_secs(300), window: Duration::from_secs(30 * 60) }
    }
}

/// 推到设备上的内容（Service Worker 的契约，见 web/public/sw.js）
#[derive(Serialize)]
struct Payload<'a> {
    title: String,
    body: String,
    /// 点开打开的页面；系统通知点开看通知页
    url: &'a str,
    session_id: Option<&'a str>,
    /// 有它时 Service Worker 给通知加「通过」「打回」按钮
    issue_id: Option<i64>,
    /// 发出那一刻的未读通知数，用作应用角标
    badge: i64,
}

fn payload(m: &PushMessage) -> Vec<u8> {
    let p = Payload {
        title: truncate(&m.title, TITLE_MAX_CHARS),
        body: truncate(&m.body, BODY_MAX_CHARS),
        url: m.url.as_deref().unwrap_or("/notifications"),
        session_id: m.session_id.as_deref(),
        issue_id: m.issue_id,
        badge: m.unread,
    };
    serde_json::to_vec(&p).expect("the push payload serializes")
}

#[derive(Debug, thiserror::Error)]
pub enum SubscribeError {
    #[error(transparent)]
    Invalid(#[from] InvalidDevice),
    #[error("database error: {0}")]
    Db(#[from] rusqlite::Error),
}

pub struct Push {
    db: Arc<Database>,
    keys: VapidKeys,
    /// VAPID 声明里的联系方式（config 的 push.vapid_subject）
    subject: String,
    client: reqwest::Client,
    retry: Retry,
}

impl Push {
    pub fn new(db: Arc<Database>, keys: VapidKeys, subject: String, client: reqwest::Client, retry: Retry) -> Self {
        Self { db, keys, subject, client, retry }
    }

    /// 浏览器订阅时用的公钥（applicationServerKey）
    pub fn public_key(&self) -> &str {
        self.keys.public_key()
    }

    /// 用户在界面上开启推送：记下这台设备（同一个 endpoint 再订阅就换成新的密钥）。
    pub async fn subscribe(&self, device: Device) -> Result<(), SubscribeError> {
        device.check()?;
        self.db
            .write(move |tx| {
                tx.execute(
                    "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at) VALUES (?1, ?2, ?3, ?4)
                     ON CONFLICT (endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth",
                    params![device.endpoint, device.p256dh, device.auth, Utc::now()],
                )
                .map(drop)
            })
            .await?;
        Ok(())
    }

    /// 把一条刚提交的通知推到此刻订阅着的每台设备（不等结果）。
    pub(crate) fn deliver(self: &Arc<Self>, notification_id: i64) {
        let push = self.clone();
        tokio::spawn(async move {
            let endpoints = match push.db.read(endpoints_in).await {
                Ok(endpoints) => endpoints,
                Err(e) => {
                    tracing::error!(notification_id, error = %e, "listing push subscriptions failed, notification not pushed");
                    return;
                }
            };
            if endpoints.is_empty() {
                tracing::info!(notification_id, "no push subscriptions, notification not pushed");
            }
            for endpoint in endpoints {
                let push = push.clone();
                tokio::spawn(async move { push.deliver_to(notification_id, endpoint).await });
            }
        });
    }

    /// 推到一台设备，暂时失败按 [`Retry`] 重试；返回最后的结局（测试用）。
    async fn deliver_to(&self, notification_id: i64, endpoint: String) -> Option<Sent> {
        let mut first_failure: Option<Instant> = None;
        let mut delay = self.retry.first;
        loop {
            let target = endpoint.clone();
            let found = self
                .db
                .read(move |c| -> rusqlite::Result<_> {
                    Ok(push_message_in(c, notification_id)?.zip(device_in(c, &target)?))
                })
                .await;
            let (message, device) = match found {
                Ok(Some(found)) => found,
                Ok(None) => {
                    // 通知随会话删了，或设备退订、已失效被删
                    tracing::info!(
                        notification_id,
                        "notification or push subscription gone before delivery, not pushing"
                    );
                    return None;
                }
                Err(e) => {
                    tracing::error!(notification_id, error = %e, "reading the notification to push failed, not pushing");
                    return None;
                }
            };
            let host = device.host();
            let sent = match webpush::send(&self.client, &self.keys, &self.subject, &device, &payload(&message)).await {
                Ok(sent) => sent,
                Err(e) => {
                    // 订阅时核对过，到这里是库里的数据坏了：删不删交给人
                    tracing::error!(notification_id, push_host = %host, error = %e, "stored push subscription is invalid, not pushing");
                    return None;
                }
            };
            match &sent {
                Sent::Delivered => tracing::info!(notification_id, push_host = %host, "web push sent"),
                Sent::Gone(status) => {
                    tracing::info!(notification_id, push_host = %host, status, "push subscription gone, removing it");
                    let gone = device.endpoint.clone();
                    if let Err(e) = self
                        .db
                        .write(move |tx| tx.execute("DELETE FROM push_subscriptions WHERE endpoint = ?1", [gone]))
                        .await
                    {
                        tracing::error!(push_host = %host, error = %e, "removing a gone push subscription failed");
                    }
                }
                Sent::Rejected(why) => {
                    tracing::error!(notification_id, push_host = %host, reason = %why, "push service rejected the message")
                }
                Sent::Transient(why) => {
                    let since = *first_failure.get_or_insert_with(Instant::now);
                    if since.elapsed() + delay <= self.retry.window {
                        tracing::warn!(notification_id, push_host = %host, reason = %why, retry_in = ?delay, "web push failed, will retry");
                        tokio::time::sleep(delay).await;
                        delay = (delay * 2).min(self.retry.max);
                        continue;
                    }
                    tracing::error!(notification_id, push_host = %host, reason = %why, "web push kept failing, giving up");
                }
            }
            return Some(sent);
        }
    }
}

fn endpoints_in(c: &Connection) -> rusqlite::Result<Vec<String>> {
    let mut stmt = c.prepare_cached("SELECT endpoint FROM push_subscriptions ORDER BY created_at")?;
    stmt.query_map([], |r| r.get(0))?.collect()
}

fn device_in(c: &Connection, endpoint: &str) -> rusqlite::Result<Option<Device>> {
    c.query_row("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE endpoint = ?1", [endpoint], |r| {
        Ok(Device { endpoint: r.get(0)?, p256dh: r.get(1)?, auth: r.get(2)? })
    })
    .optional()
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    impl Push {
        pub(crate) async fn deliver_now(&self, notification_id: i64, endpoint: &str) -> Option<Sent> {
            self.deliver_to(notification_id, endpoint.to_owned()).await
        }
    }

    #[test]
    fn payload_follows_the_service_worker_contract() {
        let m = PushMessage {
            notification_id: 1,
            title: "t".into(),
            body: "长".repeat(1000),
            url: None,
            session_id: None,
            issue_id: Some(7),
            unread: 3,
        };
        let v: serde_json::Value = serde_json::from_slice(&payload(&m)).unwrap();
        assert_eq!(v["url"], "/notifications");
        assert_eq!(v["issue_id"], 7);
        assert_eq!(v["badge"], 3);
        assert!(v["session_id"].is_null());
        assert!(v["body"].as_str().unwrap().ends_with("…[truncated: 400 more chars]"));
    }
}
