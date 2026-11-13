//! 收件箱与推送的测试：真库，推送服务用 wiremock 扮（发出的请求按 RFC 8291 解密核对内容）。

use std::sync::Arc;
use std::time::Duration;

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes128Gcm, Nonce};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;
use hkdf::Hkdf;
use muliao_platform::bus::Bus;
use muliao_platform::db::{Database, migrate};
use muliao_platform::retry::Retry;
use muliao_sessions::answers::Waiting;
use muliao_sessions::events::{RunEnded, RunFailed, SessionDeleted, Subscribers};
use muliao_sessions::origins::SessionOrigin;
use p256::elliptic_curve::rand_core::{OsRng, RngCore};
use p256::elliptic_curve::sec1::ToEncodedPoint;
use p256::{PublicKey, SecretKey};
use sha2::Sha256;
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

use crate::inbox::worth_pushing;
use crate::webpush::Sent;
use crate::*;

struct Harness {
    _dir: tempfile::TempDir,
    db: Arc<Database>,
    bus: Arc<Bus>,
    notifications: Notifications,
    subscribers: Subscribers,
}

fn fast_retry() -> Retry {
    Retry { first: Duration::from_millis(10), max: Duration::from_millis(40), window: Duration::from_millis(300) }
}

async fn harness() -> Harness {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("t.db");
    migrate(&path, &[muliao_sessions::MIGRATION, MIGRATION]).unwrap();
    let db = Arc::new(Database::open(&path, Arc::new(|| {})).unwrap());
    let bus = Arc::new(Bus::new(64));
    let keys = VapidKeys::load_or_create(&dir.path().join("vapid.pem")).unwrap();
    let push =
        Arc::new(Push::new(db.clone(), keys, "mailto:t@example.com".into(), reqwest::Client::new(), fast_retry()));
    let notifications = Notifications::new(db.clone(), bus.clone(), push);
    let mut subscribers = Subscribers::default();
    notifications.subscribe(&mut subscribers);
    Harness { _dir: dir, db, bus, notifications, subscribers }
}

impl Harness {
    async fn add_session(&self, id: &str, title: &str) {
        let (id, title) = (id.to_owned(), title.to_owned());
        self.db
            .write(move |tx| {
                tx.execute(
                    "INSERT INTO sessions (id, agent_id, title, origin, created_at, updated_at) VALUES (?1, 'a1', ?2, 'user', ?3, ?3)",
                    rusqlite::params![id, title, chrono::Utc::now()],
                )
                .map(drop)
            })
            .await
            .unwrap();
    }

    /// 像会话领域那样在一个事务里发布事件
    async fn publish<E: Send + 'static>(&self, pick: fn(&Subscribers) -> &Vec<HandlerOf<E>>, e: E) {
        let handlers = pick(&self.subscribers).clone();
        self.db
            .write(move |tx| -> Result<(), rusqlite::Error> {
                for h in &handlers {
                    h(tx, &e).unwrap();
                }
                Ok(())
            })
            .await
            .unwrap();
    }

    /// 绕过订阅时的 https 核对，让推送打到本机的 wiremock
    async fn add_device(&self, d: &Subscription) {
        let d = d.clone();
        self.db
            .write(move |tx| {
                tx.execute(
                    "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at) VALUES (?1, ?2, ?3, ?4)",
                    rusqlite::params![d.endpoint, d.p256dh, d.auth, chrono::Utc::now()],
                )
                .map(drop)
            })
            .await
            .unwrap();
    }

    async fn devices(&self) -> i64 {
        self.db.read(|c| c.query_row("SELECT count(*) FROM push_subscriptions", [], |r| r.get(0))).await.unwrap()
    }

    async fn notify(&self, title: &str, session: Option<&str>) -> i64 {
        self.notifications
            .notify(Notify {
                title: title.into(),
                body: "b".into(),
                session_id: session.map(Into::into),
                issue_id: None,
            })
            .await
            .unwrap()
    }

    /// 直接写一行通知（不经 notify，不会自动推送）：单独测一台设备的投递
    async fn quiet(&self, session: Option<&str>) -> i64 {
        let session = session.map(String::from);
        self.db
            .write(move |tx| {
                tx.execute(
                    "INSERT INTO notifications (title, body, session_id, created_at) VALUES ('t', 'b', ?1, ?2)",
                    rusqlite::params![session, chrono::Utc::now()],
                )?;
                Ok::<_, rusqlite::Error>(tx.last_insert_rowid())
            })
            .await
            .unwrap()
    }
}

type HandlerOf<E> =
    Arc<dyn Fn(&mut muliao_platform::db::Tx<'_>, &E) -> Result<(), muliao_platform::errors::Error> + Send + Sync>;

fn ended(waiting_on: Vec<Waiting>, awaited: bool, stopped: bool) -> RunEnded {
    RunEnded {
        session_id: "s1".into(),
        session_title: "标题".into(),
        origin: SessionOrigin::User,
        agent_id: "a1".into(),
        agent_name: "muliao".into(),
        run_id: 1,
        answer: "做完了".into(),
        stopped,
        waiting_on,
        awaited,
    }
}

#[test]
fn push_rule() {
    use Waiting::*;
    // 等批准：没人在等、被停止也推
    assert!(worth_pushing(&ended(vec![Approval], false, false)));
    assert!(worth_pushing(&ended(vec![AskAgent, Approval], false, true)));
    // 用户在等：做完了、在问用户推
    assert!(worth_pushing(&ended(vec![], true, false)));
    assert!(worth_pushing(&ended(vec![AskUser], true, false)));
    assert!(worth_pushing(&ended(vec![AskAgent, AskUser], true, false)));
    // 只在等 agent、用户自己停止、没人在等的不推
    assert!(!worth_pushing(&ended(vec![AskAgent], true, false)));
    assert!(!worth_pushing(&ended(vec![], true, true)));
    assert!(!worth_pushing(&ended(vec![], false, false)));
    assert!(!worth_pushing(&ended(vec![AskUser], false, false)));
}

#[tokio::test]
async fn inbox_unread_and_marking() {
    let h = harness().await;
    let mut events = h.bus.subscribe();
    let system = h.notify("系统", None).await;
    let s1 = h.notify("会话一", Some("s1")).await;
    let s2 = h.notify("会话二", Some("s2")).await;
    let review = h
        .notifications
        .notify(Notify { title: "审核".into(), body: "b".into(), session_id: Some("s3".into()), issue_id: Some(42) })
        .await
        .unwrap();

    let e = events.next().await.unwrap();
    assert_eq!((e.kind, e.data["notification_id"].as_i64()), ("notification", Some(system)));
    assert!(e.data["session_id"].is_null());
    assert_eq!(events.next().await.unwrap().data["session_id"], "s1");

    let list = h.notifications.list(10).await.unwrap();
    assert_eq!(list.iter().map(|n| n.id).collect::<Vec<_>>(), vec![review, s2, s1, system]);
    assert_eq!(list[0].url.as_deref(), Some("/issues/42"), "an issue to review opens the issue");
    assert_eq!(list[1].url.as_deref(), Some("/sessions/s2"));
    assert_eq!(list[3].url, None);
    assert_eq!(h.notifications.list(2).await.unwrap().len(), 2);
    assert_eq!(h.notifications.unread_count().await.unwrap(), 4);

    let ids: Vec<String> = ["s1", "s2", "s3", "none"].map(String::from).to_vec();
    let unread = h.notifications.unread_sessions(ids.clone()).await.unwrap();
    assert_eq!(unread.into_iter().collect::<Vec<_>>(), ["s1", "s2", "s3"]);

    h.notifications.mark_session_read("s1").await.unwrap();
    h.notifications.mark_issue_read(42).await.unwrap();
    let unread = h.notifications.unread_sessions(ids).await.unwrap();
    assert_eq!(unread.into_iter().collect::<Vec<_>>(), ["s2"], "reading the issue also reads its session's notice");
    assert_eq!(h.notifications.unread_count().await.unwrap(), 2);

    // 标到 s1 为止：之后才到的 s2 保持未读
    h.notifications.mark_read(s1).await.unwrap();
    let list = h.notifications.list(10).await.unwrap();
    assert_eq!(list.iter().filter(|n| n.unread).map(|n| n.id).collect::<Vec<_>>(), vec![s2]);
}

#[tokio::test]
async fn session_events_write_and_delete_notifications() {
    let h = harness().await;
    h.add_session("s1", "改 bug").await;

    h.publish(|s| &s.run_ended, ended(vec![], true, false)).await;
    h.publish(|s| &s.run_ended, ended(vec![Waiting::AskAgent], true, false)).await;
    let list = h.notifications.list(10).await.unwrap();
    assert_eq!(list.len(), 1, "only the answer someone waits for is pushed");
    assert_eq!((list[0].title.as_str(), list[0].body.as_str()), ("muliao · 标题", "做完了"));
    assert_eq!(list[0].session_id.as_deref(), Some("s1"));

    let failed = RunFailed {
        session_id: "s1".into(),
        agent_id: "a1".into(),
        agent_name: "muliao".into(),
        run_id: 2,
        origin: SessionOrigin::User,
        error: format!("  {}  ", "错".repeat(800)),
    };
    h.publish(|s| &s.run_failed, failed).await;
    let n = &h.notifications.list(1).await.unwrap()[0];
    assert_eq!(n.title, "muliao · 改 bug 运行失败", "the title is read from the session");
    assert_eq!(n.body, format!("{}…[truncated: 300 more chars]", "错".repeat(500)), "trimmed, then cut to 500 chars");

    let other = h.notify("别的", Some("s2")).await;
    h.publish(|s| &s.session_deleted, SessionDeleted { session_id: "s1".into(), agent_id: "a1".into() }).await;
    let left: Vec<i64> = h.notifications.list(10).await.unwrap().iter().map(|n| n.id).collect();
    assert_eq!(left, vec![other]);
}

#[tokio::test]
async fn subscribing_checks_the_device_and_replaces_keys() {
    let h = harness().await;
    let (device, _) = receiver("https://web.push.apple.com/x");
    h.notifications.push().subscribe(device.clone()).await.unwrap();
    let (again, _) = receiver("https://web.push.apple.com/x");
    h.notifications.push().subscribe(again.clone()).await.unwrap();
    assert_eq!(h.devices().await, 1);
    let stored: String =
        h.db.read(|c| c.query_row("SELECT p256dh FROM push_subscriptions", [], |r| r.get(0))).await.unwrap();
    assert_eq!(stored, again.p256dh);

    let http = Subscription { endpoint: "http://x/y".into(), ..device };
    assert!(h.notifications.push().subscribe(http).await.is_err());
    assert_eq!(h.devices().await, 1);
}

// ---- 推送 ----

/// 一台假设备：订阅信息与解密用的私钥
fn receiver(endpoint: &str) -> (Subscription, (SecretKey, Vec<u8>)) {
    let key = SecretKey::random(&mut OsRng);
    let mut auth = vec![0u8; 16];
    OsRng.fill_bytes(&mut auth);
    let device = Subscription {
        endpoint: endpoint.into(),
        p256dh: B64.encode(key.public_key().to_encoded_point(false).as_bytes()),
        auth: B64.encode(&auth),
    };
    (device, (key, auth))
}

/// 按 RFC 8291 解开请求体（设备那一侧的做法）
fn decrypt(body: &[u8], (key, auth): &(SecretKey, Vec<u8>)) -> serde_json::Value {
    let (salt, rest) = body.split_at(16);
    assert_eq!(u32::from_be_bytes(rest[..4].try_into().unwrap()), 4096);
    let id_len = rest[4] as usize;
    let (as_public, ciphertext) = rest[5..].split_at(id_len);
    let sender = PublicKey::from_sec1_bytes(as_public).unwrap();
    let shared = p256::ecdh::diffie_hellman(key.to_nonzero_scalar(), sender.as_affine());
    let mut info = b"WebPush: info\0".to_vec();
    info.extend_from_slice(key.public_key().to_encoded_point(false).as_bytes());
    info.extend_from_slice(as_public);
    let mut ikm = [0u8; 32];
    Hkdf::<Sha256>::new(Some(auth), shared.raw_secret_bytes()).expand(&info, &mut ikm).unwrap();
    let prk = Hkdf::<Sha256>::new(Some(salt), &ikm);
    let (mut cek, mut nonce) = ([0u8; 16], [0u8; 12]);
    prk.expand(b"Content-Encoding: aes128gcm\0", &mut cek).unwrap();
    prk.expand(b"Content-Encoding: nonce\0", &mut nonce).unwrap();
    let mut plain = Aes128Gcm::new(&cek.into()).decrypt(Nonce::from_slice(&nonce), ciphertext).unwrap();
    assert_eq!(plain.pop(), Some(2), "a single, last record");
    serde_json::from_slice(&plain).unwrap()
}

async fn push_service(statuses: &[u16]) -> MockServer {
    let server = MockServer::start().await;
    for (i, status) in statuses.iter().enumerate() {
        let last = i + 1 == statuses.len();
        let mock = Mock::given(method("POST")).and(path("/push/dev")).respond_with(ResponseTemplate::new(*status));
        let mock = if last { mock } else { mock.up_to_n_times(1) };
        server.register(mock.with_priority(i as u8 + 1)).await;
    }
    server
}

async fn requests(server: &MockServer) -> Vec<Request> {
    server.received_requests().await.unwrap()
}

#[tokio::test]
async fn delivers_an_encrypted_signed_message_after_commit() {
    let h = harness().await;
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/push/dev"))
        .and(header("content-encoding", "aes128gcm"))
        .and(header("ttl", "86400"))
        .respond_with(ResponseTemplate::new(201))
        .mount(&server)
        .await;
    let (device, secret) = receiver(&format!("{}/push/dev", server.uri()));
    h.add_device(&device).await;
    h.notify("旧的", None).await;
    h.notify("你好", Some("s9")).await;

    // 提交后在后台推，不等它
    let mut got = Vec::new();
    for _ in 0..200 {
        got = requests(&server).await;
        if got.len() == 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert_eq!(got.len(), 2);
    let payloads: Vec<serde_json::Value> = got.iter().map(|r| decrypt(&r.body, &secret)).collect();
    let mine = payloads.iter().find(|p| p["title"] == "你好").unwrap();
    assert_eq!(mine["url"], "/sessions/s9");
    assert_eq!(mine["session_id"], "s9");
    assert_eq!(mine["badge"], 2, "the badge is the unread count when sent");
    assert!(mine["issue_id"].is_null());
    let auth = got[0].headers.get("authorization").unwrap().to_str().unwrap();
    assert!(auth.starts_with("vapid t=") && auth.ends_with(h.notifications.push().public_key()));
}

#[tokio::test]
async fn gone_subscription_is_removed() {
    let h = harness().await;
    let server = push_service(&[410]).await;
    let (device, _) = receiver(&format!("{}/push/dev", server.uri()));
    h.add_device(&device).await;
    let id = h.quiet(None).await;
    let sent = h.notifications.push().deliver_now(id, &device.endpoint).await;
    assert_eq!(sent, Some(Sent::Gone(410)));
    assert_eq!(h.devices().await, 0);
}

#[tokio::test]
async fn transient_failures_are_retried_until_delivered() {
    let h = harness().await;
    let server = push_service(&[503, 429, 201]).await;
    let (device, _) = receiver(&format!("{}/push/dev", server.uri()));
    h.add_device(&device).await;
    let id = h.quiet(None).await;
    let sent = h.notifications.push().deliver_now(id, &device.endpoint).await;
    assert_eq!(sent, Some(Sent::Delivered));
    assert!(requests(&server).await.len() >= 3);
    assert_eq!(h.devices().await, 1);
}

#[tokio::test]
async fn retries_give_up_after_the_window() {
    let h = harness().await;
    let server = push_service(&[503]).await;
    let (device, _) = receiver(&format!("{}/push/dev", server.uri()));
    h.add_device(&device).await;
    let id = h.quiet(None).await;
    let started = std::time::Instant::now();
    let sent = h.notifications.push().deliver_now(id, &device.endpoint).await;
    assert!(matches!(sent, Some(Sent::Transient(_))), "{sent:?}");
    assert!(started.elapsed() < Duration::from_secs(2));
    assert!(requests(&server).await.len() > 2);
    assert_eq!(h.devices().await, 1, "a temporary failure keeps the subscription");
}

#[tokio::test]
async fn rejected_messages_are_not_retried_and_keep_the_subscription() {
    let h = harness().await;
    let server = push_service(&[403]).await;
    let (device, _) = receiver(&format!("{}/push/dev", server.uri()));
    h.add_device(&device).await;
    let id = h.quiet(None).await;
    let sent = h.notifications.push().deliver_now(id, &device.endpoint).await;
    assert!(matches!(sent, Some(Sent::Rejected(ref m)) if m.contains("403")), "{sent:?}");
    assert_eq!(requests(&server).await.len(), 1, "no retry");
    assert_eq!(h.devices().await, 1);
}

#[tokio::test]
async fn deleted_notifications_and_devices_are_not_pushed() {
    let h = harness().await;
    let server = push_service(&[201]).await;
    let (device, _) = receiver(&format!("{}/push/dev", server.uri()));
    h.add_session("s1", "t").await;
    let id = h.quiet(Some("s1")).await;
    h.publish(|s| &s.session_deleted, SessionDeleted { session_id: "s1".into(), agent_id: "a1".into() }).await;
    h.add_device(&device).await;
    assert_eq!(h.notifications.push().deliver_now(id, &device.endpoint).await, None);
    let other = h.quiet(None).await;
    assert_eq!(h.notifications.push().deliver_now(other, "https://gone.example/x").await, None);
}
