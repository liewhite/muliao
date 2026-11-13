//! 占用与引用它的表分开查（#804 去掉了联查）：到点唤醒按约的时刻投、没到点的不投；thread 删除后它的委派不再算欠着。

use std::sync::Arc;

use chrono::{Duration, Utc};
use muliao_platform::db::{Database, migrate};

use crate::actor::Waker;
use crate::directory::{NewSession, add_in, delete_in};
use crate::holds::{HoldOf, HoldStatus, Outside, get_in, hold_delegation_in};
use crate::mailbox::{MailBody, queued_in};
use crate::origins::SessionOrigin;
use crate::thread_calls::{owed_in, record_call_in};
use crate::wakeups::{deliver_due, pending_of_in, schedule_in};

fn db() -> (tempfile::TempDir, Arc<Database>) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("t.db");
    migrate(&path, &[crate::MIGRATION]).unwrap();
    let db = Arc::new(Database::open(&path, Arc::new(|| {})).unwrap());
    (dir, db)
}

fn session<'a>(id: &'a str, origin: SessionOrigin, parent: Option<&'a str>) -> NewSession<'a> {
    NewSession { id, agent_id: "a", title: id, origin, parent_session_id: parent }
}

#[tokio::test]
async fn due_wake_ups_are_delivered_and_the_rest_wait() {
    let (_dir, db) = db();
    let (waker, _wakes) = Waker::channel();
    let now = Utc::now();
    db.write(move |tx| {
        for s in ["s1", "s2", "s3"] {
            add_in(tx, session(s, SessionOrigin::User, None))?;
        }
        // 先约的反而晚到：按约的时刻挑，不按登记先后
        schedule_in(tx, "s3", "c3", now + Duration::hours(1), "later", false)?;
        schedule_in(tx, "s1", "c1", now - Duration::minutes(1), "first", false)?;
        schedule_in(tx, "s2", "c2", now - Duration::minutes(2), "second", false)?;
        Ok::<_, muliao_platform::errors::Error>(())
    })
    .await
    .unwrap();

    assert_eq!(deliver_due(&db, &waker, chrono_tz::UTC).await.unwrap(), 2);

    let (status, mail, pending) = db
        .read(|c| {
            let status =
                |s: &str, call: &str| get_in(c, s, &Outside::Wake { call_id: call.into() }).map(|h| h.unwrap().status);
            let notes = |s: &str| -> muliao_platform::errors::Result<Vec<String>> {
                Ok(queued_in(c, s)?
                    .into_iter()
                    .map(|m| match m.body {
                        MailBody::Event { content, .. } => content,
                        other => panic!("unexpected mail {other:?}"),
                    })
                    .collect())
            };
            Ok::<_, muliao_platform::errors::Error>((
                [status("s1", "c1")?, status("s2", "c2")?, status("s3", "c3")?],
                [notes("s1")?, notes("s2")?, notes("s3")?],
                pending_of_in(c, "s3")?.into_iter().map(|(_, _, note)| note).collect::<Vec<_>>(),
            ))
        })
        .await
        .unwrap();
    assert_eq!(status, [HoldStatus::Released, HoldStatus::Released, HoldStatus::Active]);
    assert!(mail[0].len() == 1 && mail[0][0].ends_with("Your note: first"), "{mail:?}");
    assert!(mail[1].len() == 1 && mail[1][0].ends_with("Your note: second"), "{mail:?}");
    assert!(mail[2].is_empty());
    assert_eq!(pending, ["later"]);
    // 再一轮：投过的不再投
    assert_eq!(deliver_due(&db, &waker, chrono_tz::UTC).await.unwrap(), 0);
}

#[tokio::test]
async fn a_deleted_thread_is_no_longer_owed() {
    let (_dir, db) = db();
    let owed = db
        .write(|tx| {
            add_in(tx, session("p", SessionOrigin::User, None))?;
            for t in ["t1", "t2"] {
                add_in(tx, session(t, SessionOrigin::Delegation { thread: t.into() }, Some("p")))?;
            }
            let r1 = record_call_in(tx, "p", "c1", "t1", None)?;
            let r2 = record_call_in(tx, "p", "c2", "t2", Some(60.0))?;
            hold_delegation_in(tx, "p", r1.id, Some("c1".into()), false)?;
            hold_delegation_in(tx, "p", r2.id, None, false)?;
            let before = owed_in(tx, None, Some("p"))?;
            let only_r2 = owed_in(tx, Some(&[r2.id]), None)?;
            delete_in(tx, &["t1".to_owned()])?;
            let after = owed_in(tx, None, None)?;
            Ok::<_, muliao_platform::errors::Error>((before, only_r2, after))
        })
        .await
        .unwrap();
    let (before, only_r2, after) = owed;
    let threads =
        |o: &[crate::thread_calls::Owed]| o.iter().map(|o| o.delegation.thread_id.clone()).collect::<Vec<_>>();
    assert_eq!(threads(&before), ["t1", "t2"]);
    assert_eq!(threads(&only_r2), ["t2"]);
    assert_eq!(threads(&after), ["t2"]);
    assert!(matches!(after[0].hold.of, HoldOf::Delegation { call_id: None, .. }));
}
