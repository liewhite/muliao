//! 会话领域库结构升级里换算旧数据的几步（002、003、004、006、007、008）：在那一步之前的版本插入旧形状的行，升级到那一步，用
//! 领域代码读出断言；再升级到最新，断言后面的步骤没有破坏换算结果。
//!
//! 只用程序内置的 SQLite（rusqlite bundled）：命令行 sqlite3 版本不同，对 JSON 子类型的处理也不同，拿它手验看不出 004
//! 把 muliao_parts 存成字符串的 bug。

use muliao_platform::ids::{MediaType, RunId, SessionId};
use std::collections::BTreeMap;
use std::path::PathBuf;

use crate::MIGRATION;
use crate::answers::{AgentAnswer, CallResult, Choice, Decision};
use crate::conventions::{PartKind, PartMark, part_marks};
use crate::history;
use crate::holds::{self, HoldOf};
use crate::mailbox;
use crate::origins::Origin;
use crate::work::EventSource;
use agenter::{Content, File, FileSource, Message, Request, RequestPart, ToolResult};
use chrono::Utc;
use muliao_platform::db::{Migration, migrate};
use muliao_runtime::conventions::{SharedFile, tool_attachments};
use rusqlite::{Connection, params};
use serde_json::{Value, json};

const SESSION: &str = "s1";

/// 停在某个版本的库：会话领域只跑了前 `version` 步。
struct OldDb {
    _dir: tempfile::TempDir,
    path: PathBuf,
    version: usize,
}

impl OldDb {
    /// 停在 version 的库，带一个会话。
    fn at(version: usize) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.db");
        let db = OldDb { _dir: dir, path, version: 0 };
        db.upgrade_to(version);
        let now = Utc::now();
        db.conn()
            .execute(
                "INSERT INTO sessions (id, agent_id, title, origin, created_at, updated_at) \
                 VALUES (?1, 'a', 't', 'user', ?2, ?2)",
                params![SESSION, now],
            )
            .unwrap();
        OldDb { version, ..db }
    }

    fn conn(&self) -> Connection {
        Connection::open(&self.path).unwrap()
    }

    fn upgrade_to(&self, version: usize) {
        let all: &'static [&'static str] = MIGRATION.steps;
        migrate(&self.path, &[Migration { domain: MIGRATION.domain, steps: &all[..version] }]).unwrap();
    }

    fn upgrade_to_latest(&self) {
        self.upgrade_to(MIGRATION.steps.len());
    }

    /// 一次运行（005 之前运行记录还有 kind 列）。
    fn run(&self) -> i64 {
        let c = self.conn();
        if self.version < 5 {
            c.execute(
                "INSERT INTO runs (session_id, kind, started_at) VALUES (?1, 'run', ?2)",
                params![SESSION, Utc::now()],
            )
        } else {
            c.execute("INSERT INTO runs (session_id, started_at) VALUES (?1, ?2)", params![SESSION, Utc::now()])
        }
        .unwrap();
        c.last_insert_rowid()
    }

    /// 一条用户一方的请求，metadata 原样存（旧形状由调用方给）。
    fn request(&self, run: i64, parts: Vec<RequestPart>, metadata: Option<Value>) {
        let r = Request { parts, timestamp: Utc::now(), metadata };
        history::append_in(&self.conn(), &SessionId::new(SESSION), RunId::new(run), &[Message::User(r)]).unwrap();
    }

    /// 一封已交给某次运行的信件。
    fn mail(&self, run: i64, kind: &str, origin: Option<&str>, content: &str, attachments: &str, answers: &str) {
        self.conn()
            .execute(
                "INSERT INTO mail (session_id, kind, origin, content, attachments, answers, awaited, state, taken_by, \
                 created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0, 'consumed', ?7, ?8)",
                params![SESSION, kind, origin, content, attachments, answers, run, Utc::now()],
            )
            .unwrap();
    }

    /// 各条请求记下的段（领域代码读）。
    fn marks(&self) -> Vec<Vec<PartMark>> {
        history::transcript_in(&self.conn(), &SessionId::new(SESSION), None)
            .unwrap()
            .into_iter()
            .map(|s| match s.message {
                Message::User(r) => part_marks(&r).unwrap(),
                other => panic!("expected a request, got {other:?}"),
            })
            .collect()
    }

    /// 某次运行取走的回答信件里的结果（领域代码读）。
    fn answers(&self, run: i64) -> BTreeMap<String, CallResult> {
        let mail = mailbox::consumed_by_in(&self.conn(), &SessionId::new(SESSION), RunId::new(run)).unwrap();
        let [m] = mail.as_slice() else { panic!("expected one mail, got {mail:?}") };
        m.body.answers().expect("an answer").clone()
    }
}

fn text(t: &str) -> RequestPart {
    RequestPart::Text { text: t.into() }
}

fn event_text(source: &str, body: &str) -> RequestPart {
    text(&format!("<event source=\"{source}\">\n{body}\n</event>"))
}

fn event(part: usize, source: EventSource, check_in: bool) -> PartMark {
    PartMark { part, kind: PartKind::Event { source, check_in } }
}

fn sent(part: usize, origin: Origin) -> PartMark {
    PartMark { part, kind: PartKind::Message { origin } }
}

/// 换算前的工具结果附件：派生好的地址、名字、类型、能否内联。
fn old_attachment(file_id: &str, quoted_name: &str, name: &str, media_type: &str) -> Value {
    json!({ "url": format!("/api/files/{file_id}/{quoted_name}"), "name": name, "media_type": media_type, "inline": true })
}

fn shared(file_id: &str, name: &str, media_type: &str) -> SharedFile {
    SharedFile { file_id: file_id.into(), name: name.into(), media_type: MediaType::parse(media_type).unwrap() }
}

#[test]
fn hold_kinds_are_backfilled_from_holders() {
    let db = OldDb::at(1);
    let c = db.conn();
    for (holder, call_id) in
        [(Some("job:7"), None), (Some(format!("wake:{SESSION}:c9")).as_deref(), None), (None, Some("c1"))]
    {
        c.execute(
            "INSERT INTO session_holds (session_id, holder, call_id, awaited, status, created_at) \
             VALUES (?1, ?2, ?3, 0, 'active', ?4)",
            params![SESSION, holder, call_id, Utc::now()],
        )
        .unwrap();
    }
    db.upgrade_to(2);
    // 002 时的占用还靠 holder 认，这一步只看回填的种类（现在的读法要等 008 换成整数列）
    let mut stmt = c.prepare("SELECT kind FROM session_holds ORDER BY id").unwrap();
    let kinds: Vec<String> = stmt.query_map([], |r| r.get(0)).unwrap().map(Result::unwrap).collect();
    assert_eq!(kinds, ["job", "wake", "delegation"]);
    db.upgrade_to_latest();
    let held = holds::active_among(&db.conn(), &[SessionId::new(SESSION)]).unwrap();
    let kinds: Vec<HoldOf> = held[&SessionId::new(SESSION)].iter().map(|h| h.of.clone()).collect();
    assert_eq!(
        kinds,
        [HoldOf::Job { job_id: 7 }, HoldOf::Wake, HoldOf::Delegation { row: None, call_id: Some("c1".into()) }]
    );
}

#[test]
fn holders_become_a_job_column_and_wakeups_point_at_their_hold() {
    let db = OldDb::at(7);
    let c = db.conn();
    let now = Utc::now();
    for (kind, holder) in
        [("job", "job:12"), ("wake", &*format!("wake:{SESSION}:c9")), ("wake", &*format!("wake:{SESSION}:c3"))]
    {
        let status = if holder.ends_with("c3") { "cleared" } else { "active" };
        c.execute(
            "INSERT INTO session_holds (session_id, kind, holder, awaited, status, created_at) VALUES (?1, ?2, ?3, 1, ?4, ?5)",
            params![SESSION, kind, holder, status, now],
        )
        .unwrap();
    }
    for (holder, note) in [(format!("wake:{SESSION}:c9"), "later"), (format!("wake:{SESSION}:c3"), "replaced")] {
        c.execute(
            "INSERT INTO wakeups (holder, session_id, due_at, note, created_at) VALUES (?1, ?2, ?3, ?4, ?3)",
            params![holder, SESSION, now, note],
        )
        .unwrap();
    }
    db.upgrade_to(8);
    let c = db.conn();
    let job = holds::job_in(&c, 12).unwrap().expect("the job's hold");
    assert!(job.awaited && job.status == holds::HoldStatus::Active && job.of == HoldOf::Job { job_id: 12 });
    let waiting = crate::wakeups::pending_of_in(&c, &SessionId::new(SESSION)).unwrap();
    let notes: Vec<&str> = waiting
        .iter()
        .map(|(h, _, note)| {
            assert_eq!(h.of, HoldOf::Wake);
            note.as_str()
        })
        .collect();
    assert_eq!(notes, ["later"], "only the wake-up whose hold is still active is waiting");
    let rows: i64 = c.query_row("SELECT count(*) FROM wakeups", [], |r| r.get(0)).unwrap();
    assert_eq!(rows, 2, "every wake-up row is kept");
}

#[test]
fn approval_booleans_become_a_choice() {
    let db = OldDb::at(2);
    let run = db.run();
    let answers = json!({
        "c1": { "tool": "approval", "result": { "approved": true, "always": true, "pin": "p1" } },
        "c2": { "tool": "approval", "result": { "approved": true, "always": false } },
        "c3": { "tool": "approval", "result": { "approved": false, "always": true } },
        "c4": { "tool": "approval", "result": {} },
        "c5": { "tool": "ask_agent", "result": { "text": "done" } },
    });
    db.mail(run, "answer", Some("user"), "", "[]", &answers.to_string());
    let decision = |choice, pin: &str| CallResult::Approval(Decision { choice, pin: pin.into() });
    let expected = BTreeMap::from([
        ("c1".to_owned(), decision(Choice::Always, "p1")),
        ("c2".to_owned(), decision(Choice::Approve, "")),
        ("c3".to_owned(), decision(Choice::Deny, "")),
        ("c4".to_owned(), decision(Choice::Deny, "")),
        ("c5".to_owned(), CallResult::AskAgent(AgentAnswer { text: "done".into(), attachments: vec![] })),
    ]);
    db.upgrade_to(3);
    assert_eq!(db.answers(run), expected);
    db.upgrade_to_latest();
    assert_eq!(db.answers(run), expected);
}

#[test]
fn event_indexes_and_mail_senders_become_part_marks() {
    let db = OldDb::at(3);
    let run = db.run();
    // 只有事件：来源从正文取，检查从下标表取；别的 metadata 键留着
    db.request(
        run,
        vec![text("context"), event_text("job:12", "ok"), event_text("thread:s2", "x")],
        Some(json!({ "muliao_events": [1, 2], "muliao_check_ins": [2], "other": "keep" })),
    );
    // 事件与消息都有：消息的文字与紧跟的附件段记发送方，和事件合在一起按段排
    db.request(
        run,
        vec![
            event_text("wake_me", "time"),
            text("user says hi"),
            RequestPart::File(File {
                media_type: "image/png".into(),
                source: FileSource::Url("/api/files/f1/a.png".into()),
            }),
        ],
        Some(json!({ "muliao_events": [0], "muliao_check_ins": [] })),
    );
    db.mail(run, "message", Some("user"), "user says hi", r#"["f1"]"#, "{}");
    // 没有 metadata、只有消息
    db.request(run, vec![text("from trigger")], None);
    db.mail(run, "message", Some("trigger"), "from trigger", "[]", "{}");
    // 对不上任何信件的文字
    db.request(run, vec![text("nobody sent this")], None);

    let expected = vec![
        vec![
            event(1, EventSource::Job { job_id: 12 }, false),
            event(2, EventSource::Thread { session_id: SessionId::new("s2") }, true),
        ],
        vec![event(0, EventSource::Wake, false), sent(1, Origin::User), sent(2, Origin::User)],
        vec![sent(0, Origin::Trigger)],
        vec![],
    ];
    db.upgrade_to(4);
    assert_eq!(db.marks(), expected);
    let first = history::transcript_in(&db.conn(), &SessionId::new(SESSION), None).unwrap().remove(0).message;
    let Message::User(first) = first else { unreachable!() };
    let meta = first.metadata.unwrap();
    assert_eq!(meta["other"], "keep");
    assert!(meta.get("muliao_events").is_none() && meta.get("muliao_check_ins").is_none());
    db.upgrade_to_latest();
    assert_eq!(db.marks(), expected);
}

#[test]
fn shared_files_keep_only_ids() {
    let db = OldDb::at(5);
    let run = db.run();
    let result = |call_id: &str, metadata: Option<Value>| {
        RequestPart::ToolResult(ToolResult {
            call_id: call_id.into(),
            tool_name: "share_file".into(),
            content: vec![Content::Text { text: "shared".into() }],
            is_error: false,
            metadata,
        })
    };
    db.request(
        run,
        vec![
            result(
                "t1",
                Some(json!({ "muliao_attachments": [
                    old_attachment("f1", "a%20b.png", "a b.png", "image/png"),
                    old_attachment("f2", "r.pdf", "r.pdf", "application/pdf"),
                ], "other": 1 })),
            ),
            result("t2", None),
        ],
        None,
    );
    let answers = json!({
        "c1": { "tool": "ask_agent", "result": { "text": "done", "attachments": [old_attachment("f3", "x.txt", "x.txt", "text/plain")] } },
        "c2": { "tool": "approval", "result": { "choice": "approve", "pin": null } },
    });
    db.mail(run, "answer", None, "", "[]", &answers.to_string());

    let check = || {
        let stored = history::transcript_in(&db.conn(), &SessionId::new(SESSION), None).unwrap();
        let Message::User(r) = &stored[0].message else { panic!("expected a request") };
        let files: Vec<Vec<SharedFile>> = r
            .parts
            .iter()
            .map(|p| match p {
                RequestPart::ToolResult(t) => tool_attachments(t).unwrap(),
                other => panic!("expected a tool result, got {other:?}"),
            })
            .collect();
        assert_eq!(
            files,
            [vec![shared("f1", "a b.png", "image/png"), shared("f2", "r.pdf", "application/pdf")], vec![]]
        );
        let RequestPart::ToolResult(t) = &r.parts[0] else { unreachable!() };
        assert_eq!(t.metadata.as_ref().unwrap()["other"], 1);
        assert_eq!(
            db.answers(run),
            BTreeMap::from([
                (
                    "c1".to_owned(),
                    CallResult::AskAgent(AgentAnswer {
                        text: "done".into(),
                        attachments: vec![shared("f3", "x.txt", "text/plain")]
                    })
                ),
                ("c2".to_owned(), CallResult::Approval(Decision { choice: Choice::Approve, pin: String::new() })),
            ])
        );
    };
    db.upgrade_to(6);
    check();
    db.upgrade_to_latest();
    check();
}

#[test]
fn part_marks_stored_as_strings_are_parsed_back() {
    let db = OldDb::at(6);
    let run = db.run();
    let parts = || vec![text("a"), text("b")];
    // 只有发送方：整个数组存成了字符串
    db.request(run, parts(), Some(json!({ "muliao_parts": r#"[{"part":0,"sender":"user"}]"# })));
    // 事件与发送方都有：事件那几条被套成了外层数组的一个元素，且排在后面
    db.request(
        run,
        parts(),
        Some(
            json!({ "muliao_parts": r#"[{"part":0,"sender":"issue"},[{"part":1,"event":"job:3","check_in":false}]]"# }),
        ),
    );
    // 本来就对的不动
    let right = json!({ "muliao_parts": [{ "part": 1, "sender": "user" }], "other": "keep" });
    db.request(run, parts(), Some(right.clone()));

    let expected = vec![
        vec![sent(0, Origin::User)],
        vec![sent(0, Origin::Issue), event(1, EventSource::Job { job_id: 3 }, false)],
        vec![sent(1, Origin::User)],
    ];
    db.upgrade_to(7);
    assert_eq!(db.marks(), expected);
    let stored = history::transcript_in(&db.conn(), &SessionId::new(SESSION), None).unwrap();
    let Message::User(last) = &stored[2].message else { panic!("expected a request") };
    assert_eq!(last.metadata.as_ref(), Some(&right));
    db.upgrade_to_latest();
    assert_eq!(db.marks(), expected);
}
