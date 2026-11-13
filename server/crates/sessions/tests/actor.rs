//! 会话领域端到端（离线）：真的库、资源目录、运行端口、会话 actor，模型是 wiremock 扮的 Anthropic 接口（按顺序交回事先排好的
//! 回复）。覆盖：一次普通对话、工具调用与每步保存、ask_user 挂起与回答、停止正在执行的工具、重启后被打断的一步补「被中断」、
//! ask_agent 委派与交回、最终失败与手动重试、删除忙会话被拒。

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use agenter::{Message, RequestPart, Tool, ToolCtx, ToolError, ToolOutput};
use muliao_assets::access::Actor;
use muliao_assets::distribution::Distribution;
use muliao_assets::mcp::McpRegistry;
use muliao_assets::memory::MemoryStore;
use muliao_assets::rules::ToolTable;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::{AgentService, AgentSpec};
use muliao_assets::skills::SkillStore;
use muliao_assets::source::AssetSource;
use muliao_files::FileStore;
use muliao_platform::bus::Bus;
use muliao_platform::config::Settings;
use muliao_platform::db::{Database, migrate};
use muliao_platform::tooling::{RunScope, ToolTraits, builtin};
use muliao_platform::vault::Vault;
use muliao_runtime::mcp::{Confine, McpConnector};
use muliao_runtime::models::ModelRegistry;
use muliao_runtime::toolbox::Toolbox;
use muliao_runtime::{Runtime, RuntimeDeps};
use muliao_sessions::activity::{WorkState, work_state_in};
use muliao_sessions::actor::{Actors, Waker};
use muliao_sessions::answers::QuestionAnswer;
use muliao_sessions::compaction::Compactor;
use muliao_sessions::conventions::{STOPPED_TOOL_RESULT, is_stopped};
use muliao_sessions::events::{Events, RunEnded, RunFailed, Subscribers};
use muliao_sessions::history;
use muliao_sessions::inbound::{UserMessage, UserTarget};
use muliao_sessions::live::Live;
use muliao_sessions::origins::OriginNotes;
use muliao_sessions::run::Core;
use muliao_sessions::runs::{self, RunState};
use muliao_sessions::service::SessionService;
use muliao_sessions::tools::Stopper;
use muliao_sessions::transcript::Item;
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::{Value, json};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

// ---- 扮演的模型：按顺序交回排好的回复 ----

#[derive(Clone, Default)]
struct Script(Arc<Mutex<VecDeque<ResponseTemplate>>>);

impl Script {
    fn push(&self, r: ResponseTemplate) {
        self.0.lock().unwrap().push_back(r);
    }
    fn text(&self, text: &str) {
        self.push(ok(json!({"type": "text", "text": text}), "end_turn"));
    }
    fn call(&self, id: &str, name: &str, input: Value) {
        self.push(ok(json!({"type": "tool_use", "id": id, "name": name, "input": input}), "tool_use"));
    }
}

impl Respond for Script {
    fn respond(&self, _: &Request) -> ResponseTemplate {
        self.0.lock().unwrap().pop_front().unwrap_or_else(|| panic!("the model was asked more times than scripted"))
    }
}

fn ok(block: Value, stop: &str) -> ResponseTemplate {
    ok_blocks(&[block], stop)
}

fn ok_blocks(blocks: &[Value], stop: &str) -> ResponseTemplate {
    let mut events = vec![
        json!({"type": "message_start", "message": {"model": "m", "id": "msg", "type": "message", "role": "assistant", "content": [], "stop_reason": null, "usage": {"input_tokens": 10, "output_tokens": 1}}}),
    ];
    for (i, block) in blocks.iter().enumerate() {
        if block["type"] == "tool_use" {
            events.push(json!({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}));
            events.push(json!({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": block["input"].to_string()}}));
        } else {
            events.push(
                json!({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}),
            );
            events.push(json!({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}}));
        }
        events.push(json!({"type": "content_block_stop", "index": i}));
    }
    events.push(json!({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"input_tokens": 10, "output_tokens": 2}}));
    events.push(json!({"type": "message_stop"}));
    let body: String =
        events.iter().map(|e| format!("event: {}\ndata: {e}\n\n", e["type"].as_str().unwrap())).collect();
    ResponseTemplate::new(200).set_body_string(body)
}

fn bad_request() -> ResponseTemplate {
    ResponseTemplate::new(400)
        .set_body_json(json!({"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}}))
}

// ---- 测试工具 ----

/// 等到被放行才结束的工具（测停止）；不放行就一直等
struct Slow(Arc<tokio::sync::Notify>);

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
        self.0.notified().await;
        Ok(agenter::tool::text("done"))
    }
}

struct Echo;

#[derive(Deserialize, JsonSchema)]
struct EchoArgs {
    text: String,
}

impl Tool<RunScope> for Echo {
    type Args = EchoArgs;
    fn name(&self) -> &str {
        "echo"
    }
    fn description(&self) -> &str {
        "Echo."
    }
    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: EchoArgs) -> Result<ToolOutput, ToolError> {
        Ok(agenter::tool::text(format!("echo: {}", a.text)))
    }
}

/// 参数 value 声明为要脱敏（像 vault_set），记下执行时拿到的值
struct Stash(Arc<Mutex<Vec<String>>>);

#[derive(Deserialize, JsonSchema)]
struct StashArgs {
    value: String,
}

impl Tool<RunScope> for Stash {
    type Args = StashArgs;
    fn name(&self) -> &str {
        "stash"
    }
    fn description(&self) -> &str {
        "Stash a secret."
    }
    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: StashArgs) -> Result<ToolOutput, ToolError> {
        self.0.lock().unwrap().push(a.value);
        Ok(agenter::tool::text("stashed"))
    }
}

// ---- 世界 ----

struct World {
    _dir: tempfile::TempDir,
    db: Arc<Database>,
    script: Script,
    _server: MockServer,
    service: SessionService,
    _actors: Actors,
    me: String,
    other: String,
    ended: Arc<Mutex<Vec<RunEnded>>>,
    failed: Arc<Mutex<Vec<RunFailed>>>,
    release: Arc<tokio::sync::Notify>,
    bus: Arc<Bus>,
    flaky_calls: Arc<Mutex<u32>>,
    stashed: Arc<Mutex<Vec<String>>>,
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
    let agents = Arc::new(AgentService::new(source.clone(), dir.path().join("workspaces"), "claude:m".into()));
    let files = Arc::new(FileStore::open(db.clone(), dir.path().join("files"), 1 << 20).unwrap());
    let usage = Arc::new(muliao_usage::UsageLog::new(db.clone(), chrono_tz::UTC));
    let vault = Arc::new(Vault::new("3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=").unwrap());
    let secrets = Arc::new(Secrets::new(source.clone(), vault));
    let tz = chrono_tz::Asia::Shanghai;
    let (waker, wakes) = Waker::channel();
    let stopper = Stopper::default();
    let release = Arc::new(tokio::sync::Notify::new());
    let mut tools = muliao_sessions::tools::tools(db.clone(), waker.clone(), stopper.clone(), tz);
    tools.push(builtin(Slow(release.clone()), ToolTraits::read()));
    tools.push(builtin(Echo, ToolTraits::read()));
    let flaky_calls = Arc::new(Mutex::new(0));
    tools.push(builtin(Flaky(flaky_calls.clone()), ToolTraits::read()));
    let stashed = Arc::new(Mutex::new(Vec::new()));
    tools.push(builtin(Stash(stashed.clone()), ToolTraits::read().secret_args(&["value"])));
    let toolbox = Arc::new(Toolbox::new(tools));
    binder.bind(toolbox.specs());
    let confine: Confine = Arc::new(|_agent: String| async { Err("no MCP in this test".into()) }.boxed());
    let mcp = Arc::new(McpConnector::new(Arc::new(McpRegistry::new(source.clone())), secrets, confine));
    let runtime = Arc::new(Runtime::new(RuntimeDeps {
        models,
        toolbox: toolbox.clone(),
        mcp,
        source: source.clone(),
        agents: agents.clone(),
        distribution: Arc::new(Distribution::new(source.clone())),
        memory: Arc::new(MemoryStore::new(source.clone())),
        skills: Arc::new(SkillStore::new(source.clone())),
        files: files.clone(),
        usage,
        tz,
    }));
    let me = agents
        .create(AgentSpec { name: "me".into(), description: "测试".into(), ..Default::default() }, &Actor::User)
        .await
        .unwrap()
        .id;
    let other = agents
        .create(AgentSpec { name: "other".into(), description: "另一个".into(), ..Default::default() }, &Actor::User)
        .await
        .unwrap()
        .id;
    let events = Arc::new(Events::default());
    let (ended, failed) = (Arc::new(Mutex::new(Vec::new())), Arc::new(Mutex::new(Vec::new())));
    let mut subscribers = Subscribers::default();
    let e = ended.clone();
    subscribers.on_run_ended(move |_, x| {
        e.lock().unwrap().push(x.clone());
        Ok(())
    });
    let f = failed.clone();
    subscribers.on_run_failed(move |_, x| {
        f.lock().unwrap().push(x.clone());
        Ok(())
    });
    events.subscribe(subscribers);
    let bus = Arc::new(Bus::new(1000));
    let live = Live::new(bus.clone());
    let core = Arc::new(Core {
        db: db.clone(),
        runtime: runtime.clone(),
        files,
        agents,
        live: live.clone(),
        events: events.clone(),
        notes: OriginNotes::new([
            (muliao_sessions::origins::Origin::Trigger, "A trigger started this.".to_owned()),
            (muliao_sessions::origins::Origin::Issue, "An issue started this.".to_owned()),
        ]),
        waker: waker.clone(),
        compactor: Compactor { db: db.clone(), runtime, threshold_tokens: 1_000_000, keep_steps: 10 },
        renders: toolbox.renders(),
        redaction: toolbox.redaction(),
        tz,
        bus: bus.clone(),
    });
    let actors = Actors::start(core, wakes);
    stopper.bind(actors.clone());
    actors.resume().await.unwrap();
    let service = SessionService::new(db.clone(), waker, actors.clone(), events, live, toolbox.renders(), 1_000_000);
    World {
        _dir: dir,
        bus,
        db,
        script,
        _server: server,
        service,
        _actors: actors,
        me,
        other,
        ended,
        failed,
        release,
        flaky_calls,
        stashed,
    }
}

use futures::FutureExt;

impl World {
    async fn say(&self, session: UserTarget, text: &str) -> String {
        let m = UserMessage {
            content: text.into(),
            session,
            attachments: vec![],
            answers: BTreeMap::new(),
            approvals: BTreeMap::new(),
        };
        self.service.receive(m).await.unwrap().id
    }

    async fn new_chat(&self, text: &str) -> String {
        self.say(UserTarget::NewChat { agent_id: self.me.clone() }, text).await
    }

    async fn state(&self, id: &str) -> WorkState {
        let id = id.to_owned();
        self.db.read(move |c| work_state_in(c, &id)).await.unwrap()
    }

    /// 等到会话不再 running。
    async fn settled(&self, id: &str) -> WorkState {
        for _ in 0..400 {
            let s = self.state(id).await;
            if s != WorkState::Running {
                return s;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        panic!("session {id} is still running");
    }

    async fn history(&self, id: &str) -> Vec<Message> {
        let id = id.to_owned();
        self.db
            .read(move |c| history::transcript_in(c, &id, None))
            .await
            .unwrap()
            .into_iter()
            .map(|s| s.message)
            .collect()
    }
}

fn texts(m: &Message) -> Vec<String> {
    match m {
        Message::User(r) => r
            .parts
            .iter()
            .map(|p| match p {
                RequestPart::Text { text } => text.clone(),
                RequestPart::ToolResult(t) => format!("result {}: {:?}", t.call_id, t.content),
                RequestPart::File(_) => "file".into(),
            })
            .collect(),
        Message::Assistant(r) => vec![r.text()],
    }
}

#[tokio::test]
async fn a_chat_with_a_tool_call_is_saved_step_by_step() {
    let w = world().await;
    w.script.call("c1", "echo", json!({"text": "hi"}));
    w.script.text("好了");
    let s = w.new_chat("你好").await;
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    let h = w.history(&s).await;
    assert_eq!(h.len(), 4, "{h:#?}");
    let first = texts(&h[0]);
    assert!(first[0].starts_with("[Current local time:"), "the run context comes first: {first:?}");
    assert_eq!(first[1], "你好");
    assert!(texts(&h[2])[0].contains("echo: hi"));
    let Message::User(r) = &h[2] else { panic!() };
    let RequestPart::ToolResult(t) = &r.parts[0] else { panic!() };
    assert!(muliao_sessions::conventions::finished_at(t).is_some(), "tool results carry their end time");
    assert_eq!(h[3], h[3].clone());
    let ended = w.ended.lock().unwrap().clone();
    assert_eq!(ended.len(), 1);
    assert!(ended[0].awaited && ended[0].answer == "好了" && !ended[0].stopped);
    // 第二条消息续写同一个会话：运行上下文挂在新增的第一条请求上
    w.script.text("又好了");
    w.say(UserTarget::Existing(s.clone()), "再来").await;
    w.settled(&s).await;
    let h = w.history(&s).await;
    assert_eq!(h.len(), 6);
    assert!(texts(&h[4])[0].starts_with("[Current local time:"));
}

#[tokio::test]
async fn a_secret_argument_reaches_the_tool_but_is_saved_redacted() {
    let w = world().await;
    w.script.call("c1", "stash", json!({"value": "glsa_s3cr3t"}));
    w.script.text("存好了");
    let s = w.new_chat("存一下").await;
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    assert_eq!(*w.stashed.lock().unwrap(), vec!["glsa_s3cr3t".to_owned()], "the tool gets the plain value");
    let h = w.history(&s).await;
    let Message::Assistant(r) = &h[1] else { panic!("{h:#?}") };
    let Some(agenter::ResponsePart::ToolCall(c)) =
        r.parts.iter().find(|p| matches!(p, agenter::ResponsePart::ToolCall(_)))
    else {
        panic!("{r:#?}")
    };
    assert_eq!(c.args, r#"{"value":"[redacted]"}"#, "the saved call is redacted");
}

#[tokio::test]
async fn ask_user_pauses_until_the_user_answers() {
    let w = world().await;
    let questions =
        json!({"questions": [{"question": "哪个？", "header": "选", "options": [{"label": "a"}, {"label": "b"}]}]});
    w.script.call("q1", "ask_user", questions);
    let s = w.new_chat("问我").await;
    assert_eq!(w.settled(&s).await, WorkState::Waiting, "waiting for the user's answer");
    let view = w.service.detail(&s).await.unwrap();
    assert_eq!(view.waits.len(), 1);
    assert!(w.ended.lock().unwrap()[0].answer.contains("哪个？"));
    w.script.text("你选了 a");
    let answer = UserMessage {
        content: String::new(),
        session: UserTarget::Existing(s.clone()),
        attachments: vec![],
        answers: BTreeMap::from([("q1".to_owned(), vec![QuestionAnswer { selected: vec!["a".into()], other: None }])]),
        approvals: BTreeMap::new(),
    };
    w.service.receive(answer).await.unwrap();
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    let h = w.history(&s).await;
    assert!(texts(&h[2])[0].contains("answered\\\":true"), "{:?}", texts(&h[2]));
    assert_eq!(h.last().map(|m| texts(m)[0].clone()).unwrap(), "你选了 a");
}

#[tokio::test]
async fn stopping_a_running_tool_saves_what_happened() {
    let w = world().await;
    w.script.call("c1", "slow", json!({}));
    let s = w.new_chat("慢慢来").await;
    // 等工具开始执行（回复已保存）
    for _ in 0..200 {
        if w.history(&s).await.len() == 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(w.state(&s).await, WorkState::Running);
    w.service.stop(&s).await.unwrap();
    assert_eq!(w.state(&s).await, WorkState::Idle);
    let h = w.history(&s).await;
    assert_eq!(h.len(), 3);
    assert!(is_stopped(&h[2]));
    assert!(texts(&h[2])[0].contains(STOPPED_TOOL_RESULT));
    let s2 = s.clone();
    let run = w.db.read(move |c| runs::latest_in(c, &s2)).await.unwrap().unwrap();
    assert!(matches!(run.state, RunState::Stopped { .. }), "{run:?}");
    // 停止后再说话照常跑
    w.script.text("继续");
    w.say(UserTarget::Existing(s.clone()), "接着").await;
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    assert_eq!(texts(w.history(&s).await.last().unwrap())[0], "继续");
    w.release.notify_waiters();
}

#[tokio::test]
async fn ask_agent_delegates_and_hands_the_answer_back() {
    let w = world().await;
    w.script.call("a1", "ask_agent", json!({"agent": "other", "message": "帮我算 1+1", "thread": "calc"}));
    w.script.text("2");
    w.script.text("对方说是 2");
    let s = w.new_chat("问问 other").await;
    // 发问方等回答、thread 跑完交回、发问方接着跑完
    for _ in 0..400 {
        if w.history(&s).await.len() == 4 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    let h = w.history(&s).await;
    assert!(texts(&h[2])[0].contains("\"2\"") || texts(&h[2])[0].contains("2"), "{:?}", texts(&h[2]));
    assert_eq!(texts(&h[3])[0], "对方说是 2");
    let view = w.service.detail(&s).await.unwrap();
    assert_eq!(view.threads.len(), 1);
    assert_eq!(view.threads[0].agent_id, w.other);
    let call = view
        .items
        .iter()
        .find_map(|i| match &i.item {
            Item::ToolCall(c) if c.tool_call_id == "a1" => Some(c),
            _ => None,
        })
        .unwrap();
    assert_eq!(call.threads, vec![view.threads[0].id.clone()], "the thread shows at the call that opened it");
}

#[tokio::test]
async fn a_final_failure_is_recorded_and_can_be_retried() {
    let w = world().await;
    w.script.push(bad_request());
    let s = w.new_chat("会失败").await;
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    let failed = w.failed.lock().unwrap().clone();
    assert_eq!(failed.len(), 1, "RunFailed published once");
    let view = w.service.detail(&s).await.unwrap();
    let run = view.run.expect("the failed run shows");
    assert!(run.failure().is_some());
    w.script.text("这次好了");
    w.service.retry(&s).await.unwrap();
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    assert_eq!(texts(w.history(&s).await.last().unwrap())[0], "这次好了");
    let view = w.service.detail(&s).await.unwrap();
    assert!(view.run.is_none(), "the retried run ended normally");
}

#[tokio::test]
async fn run_record_changes_tell_the_session_page() {
    let w = world().await;
    w.script.push(bad_request());
    let s = w.new_chat("会失败").await;
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    let mut events = w.bus.subscribe();
    w.script.push(bad_request());
    w.service.retry(&s).await.unwrap();
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    // 重试又失败了：没有保存新的一步，会话页照样要知道重新开始与再次失败（运行记录变了）
    assert_eq!(saved_events(&mut events, &s).await, 2, "reopened and failed again");
}

#[tokio::test]
async fn stopping_a_waiting_session_tells_its_page() {
    let w = world().await;
    let questions =
        json!({"questions": [{"question": "哪个？", "header": "选", "options": [{"label": "a"}, {"label": "b"}]}]});
    w.script.call("q1", "ask_user", questions);
    let s = w.new_chat("问我").await;
    assert_eq!(w.settled(&s).await, WorkState::Waiting);
    // 在等用户回答：没有没结束的运行可改，停止事务照样要让看着它的页面重拉
    let mut events = w.bus.subscribe();
    w.service.stop(&s).await.unwrap();
    assert_eq!(saved_events(&mut events, &s).await, 1);
}

/// 订阅到现在为止这个会话的 `saved` 有几个（等到 200ms 内没有新事件）
async fn saved_events(events: &mut muliao_platform::bus::Subscription, session: &str) -> usize {
    let mut saved = 0;
    while let Ok(Ok(e)) = tokio::time::timeout(std::time::Duration::from_millis(200), events.next()).await {
        if e.kind == "saved" && e.data["session_id"] == session {
            saved += 1;
        }
    }
    saved
}

#[tokio::test]
async fn an_interrupted_step_is_not_replayed() {
    let w = world().await;
    // 工具报暂时性故障：这一步失败，退避后从库里接着跑（和重启后一样），没出结果的调用不重新执行，补「被中断」
    w.script.call("c1", "flaky", json!({}));
    w.script.text("我看看刚才做到哪了");
    let s = w.new_chat("会被打断").await;
    // 退避期间会话页看得到上次的错误与下次尝试的时间
    let retrying = async {
        loop {
            if let Some(r) = w.service.detail(&s).await.unwrap().retrying {
                return r;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    };
    let retrying = tokio::time::timeout(std::time::Duration::from_secs(3), retrying).await.expect("shows the retry");
    assert!(!retrying.error.is_empty() && retrying.next_at > chrono::Utc::now());
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    assert!(w.service.detail(&s).await.unwrap().retrying.is_none(), "cleared once the run ended");
    let h = w.history(&s).await;
    assert!(texts(&h[2])[0].contains("Interrupted"), "{:?}", texts(&h[2]));
    assert_eq!(texts(h.last().unwrap())[0], "我看看刚才做到哪了");
    assert_eq!(*w.flaky_calls.lock().unwrap(), 1, "the interrupted call was not run again");
}

#[tokio::test]
async fn deleting_a_busy_session_is_refused() {
    let w = world().await;
    w.script.call("c1", "slow", json!({}));
    let s = w.new_chat("忙").await;
    for _ in 0..200 {
        if w.history(&s).await.len() == 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let e = w.service.delete(&s).await.unwrap_err();
    assert!(e.to_string().contains("busy"), "{e}");
    w.service.stop(&s).await.unwrap();
    w.service.delete(&s).await.unwrap();
    assert!(w.service.get(&s).await.is_err());
}

struct Flaky(Arc<Mutex<u32>>);

impl Tool<RunScope> for Flaky {
    type Args = NoArgs;
    fn name(&self) -> &str {
        "flaky"
    }
    fn description(&self) -> &str {
        "Breaks."
    }
    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, _a: NoArgs) -> Result<ToolOutput, ToolError> {
        *self.0.lock().unwrap() += 1;
        Err(ToolError::Unavailable("down for a moment".into()))
    }
}

#[tokio::test]
async fn a_question_asked_next_to_another_tool_call_resumes_with_both_results() {
    let w = world().await;
    let questions =
        json!({"questions": [{"question": "哪个？", "header": "选", "options": [{"label": "a"}, {"label": "b"}]}]});
    w.script.push(ok_blocks(
        &[
            json!({"type": "tool_use", "id": "e1", "name": "echo", "input": {"text": "先做"}}),
            json!({"type": "tool_use", "id": "q1", "name": "ask_user", "input": questions}),
        ],
        "tool_use",
    ));
    let s = w.new_chat("一边做一边问").await;
    assert_eq!(w.settled(&s).await, WorkState::Waiting);
    // 已做完的 echo 结果先存下，提问还挂着
    let h = w.history(&s).await;
    assert_eq!(h.len(), 3);
    assert!(texts(&h[2])[0].contains("echo: 先做"));
    w.script.text("知道了");
    let answer = UserMessage {
        content: "顺便说一句".into(),
        session: UserTarget::Existing(s.clone()),
        attachments: vec![],
        answers: BTreeMap::from([("q1".to_owned(), vec![QuestionAnswer { selected: vec!["b".into()], other: None }])]),
        approvals: BTreeMap::new(),
    };
    w.service.receive(answer).await.unwrap();
    assert_eq!(w.settled(&s).await, WorkState::Idle);
    let h = w.history(&s).await;
    assert_eq!(texts(h.last().unwrap())[0], "知道了");
    // 第二次请求模型时两个结果都在、只出现一次，新消息也在
    let echo_results = h.iter().flat_map(texts).filter(|t| t.contains("echo: 先做")).count();
    assert_eq!(echo_results, 1, "the earlier result is not saved twice");
    let all: Vec<String> = h.iter().flat_map(texts).collect();
    assert!(all.iter().any(|t| t.contains("result q1")), "{all:?}");
    assert!(all.iter().any(|t| t == "顺便说一句"), "{all:?}");
}
