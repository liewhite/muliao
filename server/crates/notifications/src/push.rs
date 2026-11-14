//! 推送：把收件箱（[`crate::inbox`]）里的通知推到用户的设备。设备是浏览器推送订阅（`push_subscriptions` 表只在这里读写）。
//!
//! 写下通知的事务提交后（`Tx::on_commit`）交给 [`Push::deliver`]：每条通知、每台设备一个独立的 tokio 任务，暂时失败只重试这一条，
//! 按指数退避，从第一次失败起约 30 分钟（本机到推送服务的网络常断十几分钟，#208）。重试只在内存里（#675 已认可）：服务
//! 恰好在重试期间重启，这条推送就不发了，通知本身和未读角标都还在。每次尝试前重读通知与订阅：通知随会话删了、设备退订了
//! 或已失效被删了就不再推；角标取发出那一刻的未读数。
//!
//! 不保证同一会话的推送按顺序到（Python 版按「会话 + 设备」排队）：断网期间同一会话来了两条，恢复后可能后一条先到，
//! Service Worker 按会话替换通知，锁屏上就停在前一条。罕见、点开会话即看到全部，不为它加队列。最终失败（被拒、重试放弃）
//! 只记 error 日志，由错误监控看见（Python 版是任务页上的失败任务）。

use std::sync::Arc;

use chrono::Utc;
use muliao_platform::db::Database;
use muliao_platform::errors::{Error, invalid};
use muliao_platform::retry::Retry;
use muliao_platform::text::truncate;
use rusqlite::{Connection, OptionalExtension, params};
use serde::Serialize;

use crate::inbox::{PushMessage, push_message_in};
use crate::webpush::{self, Sent, Subscription, VapidKeys};

/// 推送服务限制载荷约 4KB（加密后），标题、正文截断留足余量（中文一个字三字节）
const TITLE_MAX_CHARS: usize = 120;
const BODY_MAX_CHARS: usize = 600;

/// 推到设备上的内容（Service Worker 的契约，见 web/public/sw.js）
#[derive(Serialize)]
struct Payload<'a> {
    title: String,
    body: String,
    /// 点开打开的页面（也是通知的分组：同一页面的新通知替换旧的）；系统通知点开看通知页
    url: &'a str,
    /// 发出那一刻的未读通知数，用作应用角标
    badge: i64,
}

fn payload(m: &PushMessage) -> Vec<u8> {
    let p = Payload {
        title: truncate(&m.title, TITLE_MAX_CHARS),
        body: truncate(&m.body, BODY_MAX_CHARS),
        url: m.url.as_deref().unwrap_or("/notifications"),
        badge: m.unread,
    };
    serde_json::to_vec(&p).expect("the push payload serializes")
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

    /// 用户在界面上开启推送：解析核对（https、密钥解得开）后记下这台设备（同一个 endpoint 再订阅就换成新的密钥）。
    pub async fn subscribe(&self, sub: Subscription) -> Result<(), Error> {
        if !sub.parse().map_err(|e| invalid(e.to_string()))?.is_https() {
            return Err(invalid("invalid push subscription: endpoint must be an https URL"));
        }
        self.db
            .write(move |tx| {
                tx.execute(
                    "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at) VALUES (?1, ?2, ?3, ?4)
                     ON CONFLICT (endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth",
                    params![sub.endpoint, sub.p256dh, sub.auth, Utc::now()],
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
        let mut backoff = self.retry.start();
        loop {
            let target = endpoint.clone();
            let found = self
                .db
                .read(move |c| -> rusqlite::Result<_> {
                    Ok(push_message_in(c, notification_id)?.zip(device_in(c, &target)?))
                })
                .await;
            let (message, sub) = match found {
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
            let device = match sub.parse() {
                Ok(device) => device,
                Err(e) => {
                    // 订阅时解析过，到这里是库里的数据坏了：删不删交给人
                    tracing::error!(notification_id, error = %e, "stored push subscription is invalid, not pushing");
                    return None;
                }
            };
            let host = device.host().to_owned();
            let sent = webpush::send(&self.client, &self.keys, &self.subject, &device, &payload(&message)).await;
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
                    if let Some(delay) = backoff.next_delay() {
                        tracing::warn!(notification_id, push_host = %host, reason = %why, retry_in = ?delay, "web push failed, will retry");
                        tokio::time::sleep(delay).await;
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

fn device_in(c: &Connection, endpoint: &str) -> rusqlite::Result<Option<Subscription>> {
    c.query_row("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE endpoint = ?1", [endpoint], |r| {
        Ok(Subscription { endpoint: r.get(0)?, p256dh: r.get(1)?, auth: r.get(2)? })
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
        let m = PushMessage { notification_id: 1, title: "t".into(), body: "长".repeat(1000), url: None, unread: 3 };
        let v: serde_json::Value = serde_json::from_slice(&payload(&m)).unwrap();
        assert_eq!(v["url"], "/notifications");
        assert_eq!(v["badge"], 3);
        assert_eq!(v.as_object().unwrap().len(), 4, "title, body, url, badge: {v}");
        assert!(v["body"].as_str().unwrap().ends_with("…[truncated: 400 more chars]"));
    }
}
