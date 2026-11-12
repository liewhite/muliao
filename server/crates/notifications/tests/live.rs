//! 真推一条测试通知到本机线上库里订阅着的每台设备（会在用户的手机、电脑上弹出，默认跳过）：
//! `cargo test -p muliao-notifications --test live -- --ignored --nocapture`；只推某个推送服务的设备时设
//! `MULIAO_PUSH_HOST`（如 fcm.googleapis.com）。
//! 用线上的 VAPID 私钥（与 Python 版同一把，设备不用重新订阅）；只读打开线上库，不写任何东西（失效的订阅只报告不删）。

use muliao_notifications::webpush::{Device, Sent, VapidKeys, send};
use muliao_platform::config::{DEFAULT_CONFIG_PATH, Settings, expand_home};
use rusqlite::{Connection, OpenFlags};

#[tokio::test]
#[ignore = "pushes a real notification to the user's devices"]
async fn live_push_to_current_subscriptions() {
    let settings = Settings::load(&expand_home(DEFAULT_CONFIG_PATH)).expect("the live config parses");
    let keys = VapidKeys::load_or_create(&settings.vapid_key_path()).expect("the live VAPID key loads");
    let db = Connection::open_with_flags(settings.db_path(), OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let devices: Vec<Device> = db
        .prepare("SELECT endpoint, p256dh, auth FROM push_subscriptions")
        .unwrap()
        .query_map([], |r| Ok(Device { endpoint: r.get(0)?, p256dh: r.get(1)?, auth: r.get(2)? }))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    // 角标照线上的未读数给，免得测试把手机上的角标清零
    let unread: i64 = db.query_row("SELECT count(*) FROM notifications WHERE unread", [], |r| r.get(0)).unwrap();
    assert!(!devices.is_empty(), "no push subscriptions in the live database");

    let payload = serde_json::json!({
        "title": "Muliao（Rust 版）",
        "body": "推送测试：Rust 版后端的 Web Push 能送到这台设备。",
        "url": "/notifications",
        "session_id": null,
        "issue_id": null,
        "badge": unread,
    })
    .to_string();
    let client = muliao_platform::net::client().unwrap();
    let mut delivered = 0;
    let only = std::env::var("MULIAO_PUSH_HOST").ok();
    for device in devices.iter().filter(|d| only.as_ref().is_none_or(|h| d.host() == *h)) {
        let sent = send(&client, &keys, &settings.push.vapid_subject, device, payload.as_bytes()).await.unwrap();
        eprintln!("{}: {sent:?}", device.host());
        delivered += usize::from(sent == Sent::Delivered);
    }
    assert!(delivered > 0, "no device accepted the push");
}
