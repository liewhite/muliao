//! 触发器的测试：真库、真的会话来信入口与通知、真起命令（写沙箱 enforce）；时间由测试给（`fire_due(now)`）。

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use agenter::{Content, ToolCall, ToolCtx, ToolError};
use chrono::{DateTime, Utc};
use muliao_assets::access::Actor;
use muliao_assets::distribution::Distribution;
use muliao_assets::rules::ToolTable;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::{AgentService, AgentSpec};
use muliao_assets::skills::SkillStore;
use muliao_assets::source::AssetSource;
use muliao_files::FileStore;
use muliao_notifications::{Notifications, Push, Retry, VapidKeys};
use muliao_platform::bus::Bus;
use muliao_platform::db::{Database, migrate};
use muliao_platform::errors::ErrorKind;
use muliao_platform::sandbox::{Mode, sandbox_for};
use muliao_platform::tooling::{Audience, RunScope};
use muliao_platform::vault::Vault;
use muliao_sessions::actor::Waker;
use muliao_sessions::inbound::SenderInbox;
use muliao_sessions::mailbox::{discard_in, queued_in};
use muliao_sessions::work::SessionWork;
use muliao_workspace::{Workspace, WorkspaceDeps};
use serde_json::{Value, json};

use crate::error::Error;
use crate::precheck::{FireWhen, Precheck};
use crate::service::{NewSpec, PrecheckChange, Update};
use crate::store::{Listing, Trigger};
use crate::*;

struct World {
    dir: tempfile::TempDir,
    db: Arc<Database>,
    triggers: TriggerDomain,
    me: String,
    _wakes: tokio::sync::mpsc::UnboundedReceiver<String>,
}

fn fast() -> stream::Timing {
    stream::Timing {
        stable: Duration::from_millis(1500),
        busy_poll: Duration::from_millis(50),
        backoff: |_| Duration::from_millis(50),
    }
}

async fn world() -> World {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("t.db");
    migrate(
        &db_path,
        &[
            muliao_assets::agents::MIGRATION,
            muliao_files::MIGRATION,
            muliao_sessions::MIGRATION,
            muliao_workspace::MIGRATION,
            muliao_notifications::MIGRATION,
            MIGRATION,
        ],
    )
    .unwrap();
    let db = Arc::new(Database::open(&db_path, Arc::new(|| {})).unwrap());
    let root = dir.path().join("assets");
    std::fs::create_dir_all(root.join("platform/memory")).unwrap();
    std::fs::write(root.join("platform/memory/global.md"), "全局记忆\n").unwrap();
    let table = Arc::new(ToolTable::new());
    let source = Arc::new(AssetSource::new(db.clone(), root, 4000, Arc::new(|_, _| Ok(())), table.clone()));
    let workspaces = dir.path().join("workspaces");
    let agents = AgentService::new(source.clone(), workspaces.clone(), "claude:m".into());
    let vault = Arc::new(Vault::new("3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=").unwrap());
    let secrets = Arc::new(Secrets::new(source.clone(), vault));
    let files = Arc::new(FileStore::open(db.clone(), dir.path().join("files"), 1 << 20).unwrap());
    let home = PathBuf::from(std::env::var("HOME").unwrap());
    let (waker, wakes) = Waker::channel();
    let workspace = Workspace::build(WorkspaceDeps {
        db: db.clone(),
        sandbox: sandbox_for(Mode::Enforce, &home).unwrap(),
        workspaces_dir: workspaces,
        home,
        private_files: vec![],
        distribution: Arc::new(Distribution::new(source.clone())),
        secrets,
        assets: source.clone(),
        skills: Arc::new(SkillStore::new(source)),
        files,
        work: SessionWork::new(waker.clone()),
        max_output_chars: 10_000,
    });
    let keys = VapidKeys::load_or_create(&dir.path().join("vapid.pem")).unwrap();
    let push =
        Arc::new(Push::new(db.clone(), keys, "mailto:t@example.com".into(), reqwest::Client::new(), Retry::default()));
    let notifications = Notifications::new(db.clone(), Arc::new(Bus::new(64)), push);
    let triggers = TriggerDomain::build_with(
        TriggerDeps {
            db: db.clone(),
            tz: "Asia/Shanghai".parse().unwrap(),
            inbox: SenderInbox::new(waker),
            notifications,
            commands: workspace.commands.clone(),
            precheck_timeout_seconds: 5.0,
        },
        fast(),
    );
    let mut tools = workspace.tools();
    tools.extend(triggers.tools());
    table.bind(muliao_runtime::toolbox::Toolbox::new(tools).specs());
    let me = agents
        .create(AgentSpec { name: "me".into(), description: "测试".into(), ..Default::default() }, &Actor::User)
        .await
        .unwrap()
        .id;
    World { dir, db, triggers, me, _wakes: wakes }
}

fn at(s: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
}

impl World {
    fn svc(&self) -> &TriggerService {
        &self.triggers.service
    }

    fn ws(&self) -> PathBuf {
        self.dir.path().join("workspaces").join(&self.me)
    }

    async fn create(&self, name: &str, kind: &str, config: Value, precheck: Option<Precheck>) -> Trigger {
        self.try_create(name, kind, config, precheck).await.unwrap()
    }

    async fn try_create(
        &self,
        name: &str,
        kind: &str,
        config: Value,
        precheck: Option<Precheck>,
    ) -> Result<Trigger, Error> {
        self.svc()
            .create(NewSpec {
                name: name.into(),
                agent_id: self.me.clone(),
                kind: kind.into(),
                prompt: format!("do {name}"),
                config,
                precheck,
            })
            .await
    }

    async fn get(&self, id: i64) -> Trigger {
        self.svc().get(id).await.unwrap()
    }

    async fn status(&self, id: i64) -> Value {
        self.svc().view(self.get(id).await).await.unwrap().status
    }

    /// 这个触发器开出的会话：(id, 标题)，按建立顺序
    async fn sessions(&self, trigger_id: i64) -> Vec<(String, String)> {
        self.db
            .read(move |c| {
                let mut stmt = c.prepare(
                    "SELECT id, title FROM sessions WHERE origin = 'trigger' AND origin_ref = ?1 ORDER BY created_at, rowid",
                )?;
                stmt.query_map([trigger_id.to_string()], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()
            })
            .await
            .unwrap()
    }

    async fn mail(&self, session_id: &str) -> Vec<String> {
        let sid = session_id.to_owned();
        self.db.read(move |c| queued_in(c, &sid)).await.unwrap().into_iter().map(|m| m.content).collect()
    }

    /// 让会话空闲：丢掉排着的信
    async fn idle(&self, session_id: &str) {
        let sid = session_id.to_owned();
        self.db
            .write(move |tx| {
                let ids: Vec<i64> = queued_in(tx, &sid)?.into_iter().map(|m| m.id).collect();
                discard_in(tx, &sid, &ids).map(drop)
            })
            .await
            .unwrap();
    }

    /// 让会话在等：丢掉排着的信，登记一项占用（像后台任务）
    async fn waiting(&self, session_id: &str) {
        self.idle(session_id).await;
        let sid = session_id.to_owned();
        self.db.write(move |tx| muliao_sessions::holds::hold_in(tx, &sid, "job:1", false).map(drop)).await.unwrap();
    }

    async fn notifications(&self) -> Vec<(String, Option<String>)> {
        self.db
            .read(|c| {
                let mut stmt = c.prepare("SELECT title, session_id FROM notifications ORDER BY id")?;
                stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()
            })
            .await
            .unwrap()
    }

    async fn fire_due(&self, now: DateTime<Utc>) {
        for h in self.triggers.due.fire_due(now).await {
            h.await.unwrap();
        }
    }

    async fn archive(&self, archived: bool) {
        let me = self.me.clone();
        self.db
            .write(move |tx| {
                tx.execute(
                    "UPDATE agents SET archived_at = ?2 WHERE id = ?1",
                    rusqlite::params![me, archived.then(Utc::now)],
                )
            })
            .await
            .unwrap();
    }

    async fn call(&self, name: &str, args: Value) -> Result<String, String> {
        let tool = self.triggers.tools().into_iter().find(|t| t.name() == name).unwrap();
        let scope = RunScope::new(self.me.clone(), self.ws(), "s".into(), Audience::None);
        let args = args.to_string();
        let call = ToolCall { id: "c".into(), name: name.into(), args: args.clone(), meta: None };
        match tool.tool.call(ToolCtx { deps: &scope, call: &call, retries: 0, approval: None }, &args).await {
            Ok(content) => Ok(content
                .iter()
                .filter_map(|c| if let Content::Text { text } = c { Some(text.clone()) } else { None })
                .collect()),
            Err(ToolError::Retry(m)) => Err(m),
            Err(e) => panic!("{e:?}"),
        }
    }
}

fn check(command: &str, fire_when: FireWhen) -> Option<Precheck> {
    Some(Precheck { command: command.into(), secrets: vec![], fire_when })
}

fn kind_of(e: Error) -> ErrorKind {
    match e {
        Error::Domain(d) => d.kind,
        other => panic!("not a domain error: {other}"),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn schedule_fires_advances_and_skips_while_the_previous_session_is_busy() {
    let w = world().await;
    let t = w.create("tick", "schedule", json!({"cron": " */5 * * * * "}), None).await;
    assert_eq!(t.config, json!({"cron": "*/5 * * * *"}));
    let next = at(w.status(t.id).await["next_run_at"].as_str().unwrap());
    assert!(next > Utc::now() && next <= Utc::now() + chrono::Duration::minutes(5));

    // 不到点不触发
    w.fire_due(next - chrono::Duration::seconds(1)).await;
    assert!(w.sessions(t.id).await.is_empty());

    // 到点：开新会话，信是说明；下次时间从现在推进（停机期间错过的只补一次）
    let late = next + chrono::Duration::minutes(12);
    w.fire_due(late).await;
    let sessions = w.sessions(t.id).await;
    assert_eq!(sessions.len(), 1);
    assert!(sessions[0].1.starts_with("tick · "), "{}", sessions[0].1);
    assert_eq!(w.mail(&sessions[0].0).await, vec!["do tick"]);
    let after = w.get(t.id).await;
    assert_eq!(after.last_session_id.as_deref(), Some(sessions[0].0.as_str()));
    let following = at(w.status(t.id).await["next_run_at"].as_str().unwrap());
    assert!(following > late && following <= late + chrono::Duration::minutes(5));

    // 上一次的会话还在跑（信没处理）：跳过并记次数，不通知
    w.fire_due(following).await;
    w.fire_due(following + chrono::Duration::minutes(5)).await;
    assert_eq!(w.sessions(t.id).await.len(), 1);
    let skipped = &w.status(t.id).await["skipped"];
    assert_eq!(skipped["count"], 2);
    assert_eq!(skipped["previous_state"], "running");
    assert!(w.notifications().await.is_empty());

    // 在等（后台任务）：通知用户一次，之后不再通知
    w.waiting(&sessions[0].0).await;
    let due = at(w.status(t.id).await["next_run_at"].as_str().unwrap());
    w.fire_due(due).await;
    w.fire_due(due + chrono::Duration::minutes(5)).await;
    assert_eq!(w.status(t.id).await["skipped"]["count"], 4);
    let notes = w.notifications().await;
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert_eq!(notes[0], ("定时触发器 tick 在跳过".to_owned(), Some(sessions[0].0.clone())));

    // 空闲了：照常触发，跳过记录清掉
    let sid = sessions[0].0.clone();
    w.db.write(move |tx| tx.execute("DELETE FROM session_holds WHERE session_id = ?1", [sid])).await.unwrap();
    let due = at(w.status(t.id).await["next_run_at"].as_str().unwrap());
    w.fire_due(due).await;
    assert_eq!(w.sessions(t.id).await.len(), 2);
    assert_eq!(w.status(t.id).await["skipped"], Value::Null);
}

#[tokio::test(flavor = "multi_thread")]
async fn bad_configs_and_names_are_refused() {
    let w = world().await;
    for (kind, config) in [
        ("schedule", json!({"cron": "* * * * * *"})),
        ("schedule", json!({"cron": "0 0 30 2 *"})),
        ("schedule", json!({"cron": "*/5 * * * *", "x": 1})),
        ("once", json!({"at": "2000-01-01T00:00"})),
        ("once", json!({"at": "1767225600"})),
        ("stream", json!({"command": ""})),
        ("stream", json!({"command": "x", "debounce_seconds": -1})),
        ("nope", json!({})),
    ] {
        let e = w.try_create("t", kind, config.clone(), None).await.unwrap_err();
        assert_eq!(kind_of(e), ErrorKind::InvalidInput, "{kind} {config}");
    }
    for name in ["Tick", "-a", "a b", ""] {
        let e = w.try_create(name, "schedule", json!({"cron": "0 9 * * *"}), None).await.unwrap_err();
        assert_eq!(kind_of(e), ErrorKind::InvalidInput, "{name}");
    }
    w.create("t", "schedule", json!({"cron": "0 9 * * *"}), None).await;
    let e = w.try_create("t", "schedule", json!({"cron": "0 9 * * *"}), None).await.unwrap_err();
    assert_eq!(kind_of(e), ErrorKind::Conflict);
    // 前置检查的 secret 不存在：建的时候就报
    let p = Some(Precheck { command: "true".into(), secrets: vec!["NOPE".into()], fire_when: FireWhen::Output });
    assert!(matches!(w.try_create("u", "schedule", json!({"cron": "0 9 * * *"}), p).await, Err(Error::Domain(_))));
}

#[tokio::test(flavor = "multi_thread")]
async fn once_fires_once_then_finishes_and_leaves_the_list() {
    let w = world().await;
    let when = (Utc::now() + chrono::Duration::hours(1)).with_timezone(&chrono_tz::Asia::Shanghai);
    let t = w.create("later", "once", json!({"at": when.format("%Y-%m-%dT%H:%M").to_string()}), None).await;
    let run_at = at(w.status(t.id).await["run_at"].as_str().unwrap());
    assert!(t.config["at"].as_str().unwrap().ends_with("+08:00"));

    // 立即运行不消耗那一次
    assert!(w.svc().run(t.id).await.unwrap().is_some());
    assert!(w.get(t.id).await.finished_at.is_none());

    w.fire_due(run_at).await;
    assert_eq!(w.sessions(t.id).await.len(), 2);
    let done = w.get(t.id).await;
    assert!(done.finished_at.is_some());
    w.fire_due(run_at + chrono::Duration::hours(1)).await;
    assert_eq!(w.sessions(t.id).await.len(), 2, "fires at most once");

    assert!(w.svc().list(Listing::default()).await.unwrap().is_empty());
    assert_eq!(w.svc().list(Listing { finished: true, ..Listing::default() }).await.unwrap().len(), 1);
    assert_eq!(kind_of(w.svc().get_by_name("later").await.unwrap_err()), ErrorKind::Conflict);
    assert_eq!(kind_of(w.svc().run(t.id).await.unwrap_err()), ErrorKind::Conflict);
    let e = w.svc().update(t.id, Update { prompt: Some("x".into()), ..Update::default() }).await.unwrap_err();
    assert_eq!(kind_of(e), ErrorKind::Conflict);
    // 名字可以再用
    w.create("later", "once", json!({"at": when.to_rfc3339()}), None).await;
    // 已结束的可以按 id 删
    w.svc().delete(t.id).await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn once_due_while_archived_tells_the_user_the_task_was_not_done() {
    let w = world().await;
    let when = Utc::now() + chrono::Duration::hours(1);
    let t = w.create("later", "once", json!({"at": when.to_rfc3339()}), None).await;
    w.archive(true).await;
    w.fire_due(when).await;
    assert!(w.sessions(t.id).await.is_empty());
    assert!(w.get(t.id).await.finished_at.is_some());
    // 告警投不到归档的 agent：退给用户
    let notes = w.notifications().await;
    assert_eq!(notes, vec![("触发器 later 未执行：它的 agent 已归档".to_owned(), None)]);
    // 立即运行一个归档 agent 的触发器：报给调用方
    w.archive(false).await;
    let s = w.create("s", "schedule", json!({"cron": "0 9 * * *"}), None).await;
    w.archive(true).await;
    assert_eq!(kind_of(w.svc().run(s.id).await.unwrap_err()), ErrorKind::Conflict);
}

#[tokio::test(flavor = "multi_thread")]
async fn precheck_output_replaces_the_event_and_empty_output_skips() {
    let w = world().await;
    let t = w
        .create("p", "schedule", json!({"cron": "0 9 * * *"}), check("cat flag 2>/dev/null; true", FireWhen::Output))
        .await;
    assert_eq!(w.svc().run(t.id).await.unwrap(), None);
    assert!(w.get(t.id).await.last_checked_at.is_some());
    std::fs::create_dir_all(w.ws()).unwrap();
    std::fs::write(w.ws().join("flag"), "new mail\n").unwrap();
    let fired = w.svc().run(t.id).await.unwrap().unwrap();
    assert_eq!(w.mail(&fired.session_id).await, vec!["do p\n\n<event>\nnew mail\n\n</event>"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn changed_precheck_records_a_baseline_then_fires_on_change() {
    let w = world().await;
    std::fs::create_dir_all(w.ws()).unwrap();
    std::fs::write(w.ws().join("state"), "a\n").unwrap();
    let t = w.create("c", "schedule", json!({"cron": "0 9 * * *"}), check("cat state", FireWhen::Changed)).await;
    w.svc().fire_activated(t.id, None).await.unwrap();
    w.svc().fire_activated(t.id, None).await.unwrap();
    assert!(w.sessions(t.id).await.is_empty(), "baseline, then unchanged");
    std::fs::write(w.ws().join("state"), "b\n").unwrap();
    w.svc().fire_activated(t.id, None).await.unwrap();
    let sessions = w.sessions(t.id).await;
    assert_eq!(sessions.len(), 1);
    let mail = w.mail(&sessions[0].0).await.remove(0);
    assert!(mail.contains("<diff>\n--- previous\n+++ current\n@@ -1 +1 @@\n-a\n+b\n</diff>"), "{mail}");
    // 换检查从新基线开始
    let update = Update {
        precheck: PrecheckChange::Set(check("cat state; echo x", FireWhen::Changed).unwrap()),
        ..Update::default()
    };
    w.svc().update(t.id, update).await.unwrap();
    w.svc().fire_activated(t.id, None).await.unwrap();
    assert_eq!(w.sessions(t.id).await.len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_failing_precheck_alerts_its_agent_once_and_recovers() {
    let w = world().await;
    std::fs::create_dir_all(w.ws()).unwrap();
    let t = w
        .create(
            "f",
            "schedule",
            json!({"cron": "0 9 * * *"}),
            check("test -f ok || { echo broken >&2; exit 3; }", FireWhen::Output),
        )
        .await;

    // 立即运行：只报给调用方，不记不告警
    let e = w.svc().run(t.id).await.unwrap_err();
    assert_eq!(kind_of(e), ErrorKind::ScriptFailed);
    assert!(w.get(t.id).await.faults.is_empty());
    assert!(w.sessions(t.id).await.is_empty());

    // 自动触发：记下并告警一次（新会话给所属 agent）
    w.svc().fire_activated(t.id, None).await.unwrap();
    w.svc().fire_activated(t.id, None).await.unwrap();
    let faults = w.get(t.id).await.faults;
    assert_eq!(faults.len(), 1);
    assert!(faults[0].error.starts_with("precheck exited with 3: broken"), "{}", faults[0].error);
    let alerts = w.sessions(t.id).await;
    assert_eq!(alerts.len(), 1);
    assert!(alerts[0].1.starts_with("f 前置检查出错 · "));
    assert!(w.mail(&alerts[0].0).await[0].contains("just started failing"));

    // 恢复：清掉出错，不触发（输出为空）
    std::fs::write(w.ws().join("ok"), "").unwrap();
    w.svc().fire_activated(t.id, None).await.unwrap();
    assert!(w.get(t.id).await.faults.is_empty());
    // 再坏：重新告警
    std::fs::remove_file(w.ws().join("ok")).unwrap();
    w.svc().fire_activated(t.id, None).await.unwrap();
    assert_eq!(w.sessions(t.id).await.len(), 2);
}

#[tokio::test(flavor = "multi_thread")]
async fn update_rederives_only_on_real_changes_and_reenabling() {
    let w = world().await;
    let t = w.create("u", "schedule", json!({"cron": "0 9 * * *"}), check("exit 1", FireWhen::Output)).await;
    w.svc().fire_activated(t.id, None).await.unwrap();
    assert_eq!(w.get(t.id).await.faults.len(), 1);
    // 同样的配置（规范化后）：不重新推导，出错留着
    let same = Update { config: Some(json!({"cron": " 0 9 * * * "})), prompt: Some("new".into()), ..Update::default() };
    let u = w.svc().update(t.id, same).await.unwrap();
    assert_eq!((u.prompt.as_str(), u.faults.len(), &u.state), ("new", 1, &t.state));
    // 停用再启用：出错清掉，停用原因清掉
    w.svc().disable(t.id, "broken".into()).await.unwrap();
    assert_eq!(w.get(t.id).await.disabled_reason.as_deref(), Some("broken"));
    let u = w.svc().update(t.id, Update { enabled: Some(true), ..Update::default() }).await.unwrap();
    assert!(u.enabled && u.faults.is_empty() && u.disabled_reason.is_none());
    // 去掉检查
    let u = w.svc().update(t.id, Update { precheck: PrecheckChange::Remove, ..Update::default() }).await.unwrap();
    assert!(u.precheck.is_none());
    assert!(w.svc().run(t.id).await.unwrap().is_some());
}

#[tokio::test(flavor = "multi_thread")]
async fn corrupt_stored_state_disables_only_that_trigger() {
    let w = world().await;
    let bad = w.create("bad", "schedule", json!({"cron": "0 9 * * *"}), None).await;
    let good =
        w.create("good", "once", json!({"at": (Utc::now() + chrono::Duration::hours(1)).to_rfc3339()}), None).await;
    let id = bad.id;
    w.db.write(move |tx| tx.execute("UPDATE triggers SET state = '{\"next_run_at\": 5}' WHERE id = ?1", [id]))
        .await
        .unwrap();
    w.fire_due(Utc::now() + chrono::Duration::hours(2)).await;
    let bad = w.get(bad.id).await;
    assert!(!bad.enabled);
    assert!(bad.disabled_reason.unwrap().contains("invalid stored state"));
    // 列表照常：坏的那一行的状态是报错
    let listed: Value = serde_json::from_str(&w.call("trigger_list", json!({})).await.unwrap()).unwrap();
    assert!(listed[0]["status"]["error"].as_str().unwrap().contains("invalid stored state"), "{listed}");
    assert_eq!(w.sessions(good.id).await.len(), 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn deleting_the_agent_deletes_its_triggers() {
    let w = world().await;
    w.create("a", "schedule", json!({"cron": "0 9 * * *"}), None).await;
    let hook = w.svc().deletion_hook();
    let me = w.me.clone();
    w.db.write(move |tx| hook(tx, &me)).await.unwrap();
    assert!(w.svc().list(Listing::default()).await.unwrap().is_empty());
}

async fn eventually<F, Fut>(what: &str, mut f: F)
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    for _ in 0..200 {
        if f().await {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("timed out waiting for {what}");
}

#[tokio::test(flavor = "multi_thread")]
async fn stream_lines_are_debounced_into_one_firing_and_queue_while_busy() {
    let w = world().await;
    let config = json!({"command": "echo a; echo; echo b; sleep 0.5; echo c; sleep 60", "debounce_seconds": 0.2});
    let t = w.create("s", "stream", config, None).await;
    w.triggers.start();
    eventually("the first batch", || async { w.sessions(t.id).await.len() == 1 }).await;
    let first = w.sessions(t.id).await.remove(0).0;
    assert_eq!(w.mail(&first).await, vec!["do s\n\n<event>\na\nb\n</event>"]);
    assert!(w.status(t.id).await["running_since"].is_string());
    // 「c」在上一次的会话还忙时排着；它空闲后合并投递
    tokio::time::sleep(Duration::from_millis(800)).await;
    assert_eq!(w.sessions(t.id).await.len(), 1);
    w.idle(&first).await;
    eventually("the queued line", || async { w.sessions(t.id).await.len() == 2 }).await;
    let second = w.sessions(t.id).await.remove(1).0;
    assert_eq!(w.mail(&second).await, vec!["do s\n\n<event>\nc\n</event>"]);

    // 停用：进程组被杀
    w.svc().update(t.id, Update { enabled: Some(false), ..Update::default() }).await.unwrap();
    eventually("the process to be killed", || async {
        let out = std::process::Command::new("pgrep").args(["-f", "[s]leep 60"]).output().unwrap();
        out.stdout.is_empty()
    })
    .await;
    assert_eq!(w.status(t.id).await["running_since"], Value::Null);
    w.triggers.shutdown().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stream_that_keeps_exiting_alerts_once_with_its_stderr() {
    let w = world().await;
    let t = w.create("crash", "stream", json!({"command": "echo boom >&2; exit 2"}), None).await;
    w.triggers.start();
    eventually("the alert", || async { !w.sessions(t.id).await.is_empty() }).await;
    let faults = w.get(t.id).await.faults;
    assert_eq!(faults.len(), 1);
    assert!(faults[0].error.contains("exited quickly 3 times in a row"), "{}", faults[0].error);
    assert!(faults[0].error.contains("stream command exited with 2: "), "{}", faults[0].error);
    assert!(faults[0].error.contains("boom"));
    let alert = w.sessions(t.id).await.remove(0);
    assert!(alert.1.starts_with("crash 流式命令反复退出"));
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(w.sessions(t.id).await.len(), 1, "alerted once");
    let log = std::fs::read_to_string(w.ws().join("trigger-logs/crash.stderr.log")).unwrap();
    assert!(log.contains("--- started ") && log.contains("boom"));
    assert!(w.status(t.id).await["failures"].as_u64().unwrap() >= 3);
    w.triggers.shutdown().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn tools_manage_triggers_by_name() {
    let w = world().await;
    let created = w
        .call(
            "trigger_create",
            json!({"name": "daily", "kind": "schedule", "prompt": "report", "config": {"cron": "0 9 * * *"}}),
        )
        .await
        .unwrap();
    assert!(created.starts_with("created schedule trigger daily; status: {\"next_run_at\":"), "{created}");
    let listed: Value = serde_json::from_str(&w.call("trigger_list", json!({})).await.unwrap()).unwrap();
    assert_eq!(listed[0]["name"], "daily");
    assert_eq!(listed[0]["agent"], "me");
    assert_eq!(listed[0]["faults"], json!([]));
    let kinds: Value = serde_json::from_str(&w.call("trigger_kinds", json!({})).await.unwrap()).unwrap();
    assert_eq!(kinds.as_array().unwrap().len(), 3);
    let both = json!({"name": "daily", "precheck": {"command": "true"}, "remove_precheck": true});
    assert!(w.call("trigger_update", both).await.unwrap_err().contains("not both"));
    let paused = w.call("trigger_update", json!({"name": "daily", "enabled": false})).await.unwrap();
    assert!(paused.starts_with("updated trigger daily (paused)"));
    assert!(w.call("trigger_run", json!({"name": "daily"})).await.unwrap_err().contains("disabled"));
    w.call("trigger_update", json!({"name": "daily", "enabled": true})).await.unwrap();
    assert!(
        w.call("trigger_run", json!({"name": "daily"})).await.unwrap().starts_with("fired trigger daily: session ")
    );
    assert!(
        w.call(
            "trigger_create",
            json!({"name": "x", "kind": "schedule", "prompt": "p", "config": {"cron": "0 9 * * *"}, "agent": "ghost"})
        )
        .await
        .unwrap_err()
        .contains("no agent named")
    );
    assert_eq!(w.call("trigger_delete", json!({"name": "daily"})).await.unwrap(), "deleted trigger daily");
    assert!(w.call("trigger_delete", json!({"name": "daily"})).await.unwrap_err().contains("not found"));
}
