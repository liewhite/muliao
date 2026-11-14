//! issue 看板端到端（离线）：真的库、资源目录、运行端口、会话 actor 与 issue 工具，模型是 wiremock 扮的 Anthropic 接口（按
//! 顺序交回排好的回复）。覆盖：运行结束时 issue 没表态就请它表态、它在自己的 issue 会话里交给用户；issue_stop 停掉正在跑
//! 的会话并把收尾消息投进去。

use muliao_platform::ids::{AgentId, SessionId};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use agenter::{Tool, ToolCtx, ToolError, ToolOutput};
use futures::FutureExt;
use muliao_assets::access::Actor;
use muliao_assets::rules::ToolTable;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::{AgentService, AgentSpec};
use muliao_assets::source::AssetSource;
use muliao_files::FileStore;
use muliao_issues::activity::Happening;
use muliao_issues::{Assignee, Board, By, NewIssue, Priority, Status};
use muliao_notifications::{Notifications, Push, VapidKeys};
use muliao_platform::bus::Bus;
use muliao_platform::config::Settings;
use muliao_platform::db::{Database, migrate};
use muliao_platform::retry::Retry;
use muliao_platform::tooling::{RunScope, ToolTraits, builtin};
use muliao_platform::vault::Vault;
use muliao_runtime::mcp::{Confine, McpConnector};
use muliao_runtime::models::ModelRegistry;
use muliao_runtime::toolbox::Toolbox;
use muliao_runtime::{Runtime, RuntimeDeps};
use muliao_sessions::activity::{WorkState, work_state_in};
use muliao_sessions::actor::{Actors, Waker};
use muliao_sessions::compaction::Compactor;
use muliao_sessions::events::{Events, Subscribers};
use muliao_sessions::inbound::SenderInbox;
use muliao_sessions::live::Live;
use muliao_sessions::origins::{Origin, OriginNotes};
use muliao_sessions::run::Core;
use muliao_sessions::tools::Stopper;
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::{Value, json};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

// ---- 扮演的模型：按顺序交回排好的回复，并记下每次请求 ----

#[derive(Clone, Default)]
struct Script {
    replies: Arc<Mutex<VecDeque<ResponseTemplate>>>,
    asked: Arc<Mutex<Vec<Value>>>,
}

impl Script {
    fn text(&self, text: &str) {
        self.replies.lock().unwrap().push_back(ok(json!({"type": "text", "text": text}), "end_turn"));
    }
    fn call(&self, id: &str, name: &str, input: Value) {
        let block = json!({"type": "tool_use", "id": id, "name": name, "input": input});
        self.replies.lock().unwrap().push_back(ok(block, "tool_use"));
    }
    fn left(&self) -> usize {
        self.replies.lock().unwrap().len()
    }
}

impl Respond for Script {
    fn respond(&self, r: &Request) -> ResponseTemplate {
        self.asked.lock().unwrap().push(serde_json::from_slice(&r.body).unwrap());
        self.replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| panic!("the model was asked more times than scripted"))
    }
}

fn ok(block: Value, stop: &str) -> ResponseTemplate {
    let mut events = vec![
        json!({"type": "message_start", "message": {"model": "m", "id": "msg", "type": "message", "role": "assistant", "content": [], "stop_reason": null, "usage": {"input_tokens": 10, "output_tokens": 1}}}),
    ];
    if block["type"] == "tool_use" {
        events.push(json!({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}));
        events.push(json!({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": block["input"].to_string()}}));
    } else {
        events.push(json!({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}));
        events.push(
            json!({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": block["text"]}}),
        );
    }
    events.push(json!({"type": "content_block_stop", "index": 0}));
    events.push(json!({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"input_tokens": 10, "output_tokens": 2}}));
    events.push(json!({"type": "message_stop"}));
    let body: String =
        events.iter().map(|e| format!("event: {}\ndata: {e}\n\n", e["type"].as_str().unwrap())).collect();
    ResponseTemplate::new(200).set_body_string(body)
}

/// 一直等着的工具（测停止）
struct Slow;

#[derive(Deserialize, JsonSchema)]
struct NoArgs {}

impl Tool<RunScope> for Slow {
    type Args = NoArgs;
    fn name(&self) -> &str {
        "slow"
    }
    fn description(&self) -> &str {
        "Wait."
    }
    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, _a: NoArgs) -> Result<ToolOutput, ToolError> {
        futures::future::pending::<()>().await;
        unreachable!()
    }
}

struct World {
    _dir: tempfile::TempDir,
    db: Arc<Database>,
    script: Script,
    _server: MockServer,
    board: Board,
    _actors: Actors,
    me: AgentId,
    other: AgentId,
    notifications: Notifications,
}

async fn world() -> World {
    let server = MockServer::start().await;
    let script = Script::default();
    Mock::given(method("POST")).and(path("/v1/messages")).respond_with(script.clone()).mount(&server).await;
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("t.db");
    migrate(
        &db_path,
        &[
            muliao_assets::agents::MIGRATION,
            muliao_files::MIGRATION,
            muliao_usage::MIGRATION,
            muliao_sessions::MIGRATION,
            muliao_notifications::MIGRATION,
            muliao_issues::MIGRATION,
        ],
    )
    .unwrap();
    let db = Arc::new(Database::open(&db_path, Arc::new(|| {})).unwrap());
    let root = dir.path().join("assets");
    std::fs::create_dir_all(root.join("platform/memory")).unwrap();
    std::fs::write(root.join("platform/memory/global.md"), "全局记忆\n").unwrap();
    let config = format!(
        "[server]\npublic_url = \"http://127.0.0.1:1080\"\n[auth]\npassword = \"12345678\"\nsecret_key = \
         \"0123456789abcdef0123456789abcdef\"\n[vault]\nkey = \"k\"\n[push]\nvapid_subject = \"mailto:a@b.c\"\n[llm]\n\
         default_model = \"claude:m\"\n[llm.providers.claude]\ntype = \"anthropic\"\napi_key = \"k\"\nbase_url = \"{}\"\n",
        server.uri()
    );
    let settings = Settings::parse(&config, dir.path().join("config.toml")).unwrap();
    let models = Arc::new(ModelRegistry::new(&settings.llm, reqwest::Client::new()));
    let (table, binder) = ToolTable::pending();
    let source = Arc::new(AssetSource::new(db.clone(), root.clone(), 4000, models.model_check(), table.clone()));
    let agents = Arc::new(AgentService::new(
        source.clone(),
        dir.path().join("workspaces"),
        muliao_platform::config::ModelRef::parse("claude:m").unwrap(),
    ));
    let files = Arc::new(FileStore::open(db.clone(), dir.path().join("files"), 1 << 20).unwrap());
    let usage = Arc::new(muliao_usage::UsageLog::new(db.clone(), chrono_tz::UTC));
    let vault = Arc::new(Vault::new("3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=").unwrap());
    let secrets = Arc::new(Secrets::new(source.clone(), vault));
    let tz = chrono_tz::Asia::Shanghai;
    let (waker, wakes) = Waker::channel();
    let stopper = Stopper::default();
    let keys = VapidKeys::load_or_create(&dir.path().join("vapid.pem")).unwrap();
    let push = Push::new(db.clone(), keys, "mailto:t@example.com".into(), reqwest::Client::new(), Retry::default());
    let notifications = Notifications::new(db.clone(), Arc::new(Bus::new(16)), Arc::new(push));
    let board = Board::new(db.clone(), SenderInbox::new(waker.clone()), notifications.clone(), stopper.clone(), tz);
    let mut tools = muliao_sessions::tools::tools(db.clone(), waker.clone(), stopper.clone(), tz);
    tools.extend(muliao_issues::tools::tools(board.clone()));
    tools.push(builtin(Slow, ToolTraits::read()));
    let toolbox = Arc::new(Toolbox::new(tools));
    binder.bind(toolbox.specs());
    let confine: Confine =
        Arc::new(|_agent: AgentId| async { Err(muliao_platform::errors::broken("no MCP in this test")) }.boxed());
    let mcp = Arc::new(McpConnector::new(secrets, confine));
    let runtime = Arc::new(Runtime::new(RuntimeDeps {
        models,
        toolbox: toolbox.clone(),
        mcp,
        source: source.clone(),
        agents: agents.clone(),
        files: files.clone(),
        usage,
        tz,
    }));
    let spec = |name: &str| AgentSpec { name: name.into(), description: "测试".into(), ..Default::default() };
    let me = agents.create(spec("me"), &Actor::User).await.unwrap().id;
    let other = agents.create(spec("other"), &Actor::User).await.unwrap().id;
    let events = Arc::new(Events::default());
    let mut subscribers = Subscribers::default();
    board.subscribe(&mut subscribers);
    events.subscribe(subscribers);
    let bus = Arc::new(Bus::new(1000));
    let core = Arc::new(Core {
        db: db.clone(),
        runtime: runtime.clone(),
        files,
        agents,
        live: Live::new(bus.clone()),
        events,
        notes: OriginNotes::new([
            (Origin::Trigger, "A trigger started this.".to_owned()),
            (Origin::Issue, muliao_issues::rules::SESSION_NOTE.to_owned()),
        ]),
        waker,
        compactor: Compactor { db: db.clone(), runtime, threshold_tokens: 1_000_000, keep_steps: 10 },
        renders: toolbox.renders(),
        redaction: toolbox.redaction(),
        tz,
        bus,
    });
    let actors = Actors::start(core, wakes);
    stopper.bind(actors.clone());
    actors.resume().await.unwrap();
    World { _dir: dir, db, script, _server: server, board, _actors: actors, me, other, notifications }
}

impl World {
    async fn session(&self, issue_id: i64) -> SessionId {
        for _ in 0..200 {
            if let Some(s) = self.board.get(issue_id).await.unwrap().work.sessions.get(&self.me) {
                return s.clone();
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        panic!("no issue session for #{issue_id}");
    }

    async fn state(&self, id: &SessionId) -> WorkState {
        let id = id.clone();
        self.db.read(move |c| work_state_in(c, &id)).await.unwrap()
    }

    /// 等到排好的回复都用完、会话不再 running
    async fn settled(&self, id: &SessionId) {
        for _ in 0..400 {
            if self.script.left() == 0 && self.state(id).await != WorkState::Running {
                return;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        panic!("session {id} did not settle; {} replies left", self.script.left());
    }

    async fn kinds(&self, issue_id: i64) -> Vec<String> {
        self.board.detail(issue_id).await.unwrap().activities.iter().map(|a| a.what.kind()).collect()
    }
}

fn work_for_me(w: &World, title: &str) -> NewIssue {
    NewIssue {
        owner: w.me.clone(),
        title: title.into(),
        status: Status::InProgress,
        assignee: Some(Assignee::Agent { agent_id: w.me.clone() }),
        description: String::new(),
        priority: Priority::default(),
        depends_on: vec![],
        key: None,
    }
}

#[tokio::test]
async fn an_agent_that_stops_without_settling_is_asked_until_it_hands_the_issue_over() {
    let w = world().await;
    w.script.text("我看完了");
    w.script.call("u1", "issue_update", json!({"issue_id": 1, "status": "in_review", "assignee": "user"}));
    w.script.text("交给用户了");
    let i = w.board.create_many(By::user(), vec![work_for_me(&w, "要表态")]).await.unwrap().remove(0);
    assert_eq!(i.id, 1);
    let s = w.session(i.id).await;
    w.settled(&s).await;
    assert_eq!(w.kinds(i.id).await, ["created", "unsettled", "status", "assignee"]);
    let asked = w.script.asked.lock().unwrap().clone();
    assert_eq!(asked.len(), 3);
    let system = asked[0]["system"].to_string();
    assert!(system.contains("This conversation is dedicated to one issue"), "the issue note is in the instructions");
    assert!(
        asked[1].to_string().contains("a comment alone does not settle it"),
        "the second run is the request to settle"
    );
    let issue = w.board.get(i.id).await.unwrap().issue;
    assert_eq!((issue.status, issue.assignee), (Status::InReview, Some(Assignee::User)));
    let notices = w.notifications.list(10).await.unwrap();
    assert_eq!(notices.len(), 1);
    assert_eq!(notices[0].session_id.as_ref(), Some(&s), "the notice points at the issue session");
}

#[tokio::test]
async fn issue_stop_stops_the_running_session_and_delivers_the_message_there() {
    let w = world().await;
    w.script.call("c1", "slow", json!({}));
    let i = w.board.create_many(By::user(), vec![work_for_me(&w, "做到一半")]).await.unwrap().remove(0);
    let s = w.session(i.id).await;
    for _ in 0..200 {
        if w.script.left() == 0 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(w.state(&s).await, WorkState::Running);
    // 收尾那一轮：它在自己的 issue 会话里把 issue 挂起给用户
    w.script.call("u1", "issue_update", json!({"issue_id": i.id, "status": "blocked", "assignee": "user"}));
    w.script.text("停了");
    let stopping = w
        .board
        .stop(
            By::agent(&w.other, &SessionId::new("elsewhere")),
            i.id,
            muliao_issues::rules::StopTo::Redirect("先停下，等用户".into()),
        )
        .await
        .unwrap();
    assert!(stopping.stopped);
    assert_eq!(stopping.agent, w.me);
    w.settled(&s).await;
    assert_eq!(w.kinds(i.id).await, ["created", "stopped", "comment", "status", "assignee"]);
    let detail = w.board.detail(i.id).await.unwrap();
    let Happening::Stopped { change } = &detail.activities[1].what else { panic!() };
    assert_eq!(change.to, w.me);
    let asked = w.script.asked.lock().unwrap().clone();
    assert!(asked[1].to_string().contains("先停下，等用户"), "the message reached the stopped session");
    assert_eq!(detail.issue.issue.status, Status::Blocked);
}
