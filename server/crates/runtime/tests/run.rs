//! 运行端口端到端（离线）：真的资源目录、库、文件存储，模型是 wiremock 扮的 Anthropic 接口。一次运行从组装（工具按规则开放、
//! MCP 连不上的跳过并告诉模型、没加载的资源文件告诉模型）走到结束：附件展开后发给模型、工具交回的图片存成引用、工具挂上的
//! 附加数据并进结果、每次回复记用量；压缩记为压缩的用量。

use muliao_platform::ids::{AgentId, MediaType, SessionId};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use agenter::{
    Content, FileSource, Message, Request, RequestPart, Resolution, Tool, ToolCtx, ToolError, ToolOutput, ToolsDone,
};
use chrono::Utc;
use futures::FutureExt;
use muliao_assets::access::Actor;
use muliao_assets::mcp::McpRegistry;
use muliao_assets::rules::ToolTable;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::{AgentService, AgentSpec};
use muliao_assets::source::AssetSource;
use muliao_files::FileStore;
use muliao_platform::config::Settings;
use muliao_platform::db::{Database, migrate};
use muliao_platform::sandbox::{Category, Confinement, Sandbox, WritablePaths};
use muliao_platform::tooling::{Audience, RunScope, ToolTraits, builtin};
use muliao_platform::vault::Vault;
use muliao_runtime::conventions::{ResultFiles, SharedFile, attach};
use muliao_runtime::mcp::{Confine, McpConnector};
use muliao_runtime::models::ModelRegistry;
use muliao_runtime::toolbox::Toolbox;
use muliao_runtime::{RunSpec, Runtime, RuntimeDeps, ToolsRun};
use muliao_usage::UsageLog;
use schemars::JsonSchema;
use serde::Deserialize;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const PNG: &[u8] = &[
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89, 0x00, 0x00, 0x00, 0x0d, 0x49,
    0x44, 0x41, 0x54, 0x78, 0xda, 0x63, 0xfc, 0xcf, 0xc0, 0xf0, 0x1f, 0x00, 0x05, 0x05, 0x02, 0x00, 0x5f, 0xc8, 0xf1,
    0xd2, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
];

/// 一个测试工具：交回一张图片，并给结果挂上一个附件
struct Snap {
    attachment: SharedFile,
}

#[derive(Deserialize, JsonSchema)]
struct SnapArgs {
    #[allow(dead_code)]
    token: String,
}

impl Tool<RunScope> for Snap {
    type Args = SnapArgs;
    fn name(&self) -> &str {
        "snap"
    }
    fn description(&self) -> &str {
        "Take a picture."
    }
    async fn call(&self, ctx: ToolCtx<'_, RunScope>, _a: SnapArgs) -> Result<ToolOutput, ToolError> {
        attach(ctx.deps, &ctx.call.id, std::slice::from_ref(&self.attachment));
        Ok(vec![
            Content::text("took it"),
            Content::File(agenter::File { media_type: "image/png".into(), source: FileSource::Data(PNG.to_vec()) }),
        ])
    }
}

/// 只给这个测试 agent 的工具：不在 base 里（admin 类），规则没放行就不开放
struct Hidden;

#[derive(Deserialize, JsonSchema)]
struct NoArgs {}

impl Tool<RunScope> for Hidden {
    type Args = NoArgs;
    fn name(&self) -> &str {
        "hidden"
    }
    fn description(&self) -> &str {
        "Admin only."
    }
    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, _a: NoArgs) -> Result<ToolOutput, ToolError> {
        Ok(agenter::tool::text("x"))
    }
}

/// 假的写沙箱：在命令前面加一个不存在的程序，连接失败的原因里能看出命令被包过
struct FakeSandbox;

impl Sandbox for FakeSandbox {
    fn paths(&self, _category: Category) -> Vec<PathBuf> {
        vec![]
    }
    fn wrap(&self, argv: &[String], _w: &WritablePaths, _u: &[muliao_platform::fs::RealPath]) -> Vec<String> {
        std::iter::once("/nonexistent/wrapped".to_owned()).chain(argv.iter().cloned()).collect()
    }
}

fn sse(events: &[serde_json::Value]) -> String {
    events.iter().map(|e| format!("event: {}\ndata: {e}\n\n", e["type"].as_str().unwrap())).collect()
}

fn message(content: serde_json::Value, stop: &str, input: u64, output: u64) -> String {
    use serde_json::json;
    let mut events = vec![
        json!({"type": "message_start", "message": {"model": "m", "id": "msg_1", "type": "message", "role": "assistant", "content": [], "stop_reason": null, "usage": {"input_tokens": input, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 1}}}),
    ];
    let block = &content;
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
    events.push(json!({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"input_tokens": input, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": output}}));
    events.push(json!({"type": "message_stop"}));
    sse(&events)
}

struct World {
    _dir: tempfile::TempDir,
    root: PathBuf,
    runtime: Runtime,
    files: Arc<FileStore>,
    usage: Arc<UsageLog>,
    agents: Arc<AgentService>,
    mcp: Arc<McpConnector>,
    confined: Arc<Mutex<Vec<AgentId>>>,
}

async fn world(server: &MockServer) -> World {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("t.db");
    migrate(&db_path, &[muliao_assets::agents::MIGRATION, muliao_files::MIGRATION, muliao_usage::MIGRATION]).unwrap();
    let db = Arc::new(Database::open(&db_path, Arc::new(|| {})).unwrap());
    let root = dir.path().join("assets");
    std::fs::create_dir_all(root.join("platform/memory")).unwrap();
    std::fs::write(root.join("platform/memory/global.md"), "全局记忆\n").unwrap();
    let config = format!(
        r#"
[server]
public_url = "http://127.0.0.1:1080"
[auth]
password = "12345678"
secret_key = "0123456789abcdef0123456789abcdef"
[vault]
key = "k"
[push]
vapid_subject = "mailto:a@b.c"
[llm]
default_model = "claude:m"
[llm.providers.claude]
type = "anthropic"
api_key = "test-key"
base_url = "{}"
"#,
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
    let usage = Arc::new(UsageLog::new(db.clone(), chrono_tz::UTC));
    let vault = Arc::new(Vault::new("3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=").unwrap());
    let secrets = Arc::new(Secrets::new(source.clone(), vault));
    let attachment = SharedFile {
        file_id: "f1".into(),
        name: "report.pdf".into(),
        media_type: MediaType::parse("application/pdf").unwrap(),
    };
    let toolbox = Arc::new(Toolbox::new(vec![
        builtin(Snap { attachment }, ToolTraits::read().secret_args(&["token"])),
        builtin(Hidden, ToolTraits::write().admin()),
    ]));
    binder.bind(toolbox.specs());
    let confined = Arc::new(Mutex::new(Vec::new()));
    let seen = confined.clone();
    let home = dir.path().to_path_buf();
    let confine: Confine = Arc::new(move |agent: AgentId| {
        seen.lock().unwrap().push(agent.clone());
        let home = home.clone();
        async move {
            Ok(Confinement {
                sandbox: Arc::new(FakeSandbox),
                writable: WritablePaths::of(vec![], agent),
                home,
                explain: Arc::new(|_: &Path| String::new()),
                unreadable: vec![],
            })
        }
        .boxed()
    });
    let mcp = Arc::new(McpConnector::new(secrets, confine));
    let runtime = Runtime::new(RuntimeDeps {
        models,
        toolbox,
        mcp: mcp.clone(),
        source: source.clone(),
        agents: agents.clone(),
        files: files.clone(),
        usage: usage.clone(),
        tz: chrono_tz::Asia::Shanghai,
    });
    World { _dir: dir, root, runtime, files, usage, agents, mcp, confined }
}

fn write(path: &Path, text: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

fn user(parts: Vec<RequestPart>) -> Message {
    Message::User(Request { parts, timestamp: Utc::now(), metadata: None })
}

#[tokio::test]
async fn a_run_from_start_to_finish() {
    let server = MockServer::start().await;
    let w = world(&server).await;
    let me = w
        .agents
        .create(AgentSpec { name: "me".into(), description: "测试 agent".into(), ..Default::default() }, &Actor::User)
        .await
        .unwrap()
        .id;
    // 三个 MCP server：一个引用了没有的 secret、一个 http 连不上、一个 stdio（命令被包进写沙箱后起不来）
    write(&w.root.join("me/mcp/nokey.json"), r#"{"transport": "http", "url": "http://127.0.0.1:9/${vault:NOPE}"}"#);
    write(&w.root.join("me/mcp/down.json"), r#"{"transport": "http", "url": "http://127.0.0.1:9/mcp"}"#);
    write(&w.root.join("me/mcp/local.json"), r#"{"transport": "stdio", "command": "srv", "args": ["-x"]}"#);
    // 一个读坏的 skill
    write(&w.root.join("me/skills/broken/SKILL.md"), "no frontmatter\n");

    let assets = w.runtime.run_assets(&me).await.unwrap();
    let run = w
        .runtime
        .start(RunSpec {
            assets: &assets,
            session_id: &SessionId::new("s1"),
            audience: Audience::User,
            origin_note: "Started by the user.",
            summary: None,
        })
        .await
        .unwrap();
    let tools: Vec<&str> = run.tools().iter().map(|t| t.name.as_str()).collect();
    assert_eq!(tools, ["snap"], "admin tools are not in base");
    let notices = run.notices().join("\n");
    assert!(notices.contains("Asset file not loaded:") && notices.contains("SKILL.md"), "{notices}");
    assert!(notices.contains("MCP server \"nokey\"") && notices.contains("NOPE"), "{notices}");
    assert!(notices.contains("MCP server \"down\" failed"), "{notices}");
    assert!(notices.contains("MCP server \"local\" failed") && notices.contains("/nonexistent/wrapped"), "{notices}");
    assert_eq!(
        *w.confined.lock().unwrap(),
        std::slice::from_ref(&me),
        "the stdio server is confined as the agent using it"
    );

    // 用户发来一张图片附件
    let meta = w.files.put("photo.png", MediaType::parse("image/png").unwrap(), PNG).await.unwrap();
    let mut history = vec![user(
        std::iter::once(RequestPart::Text { text: "看看".into() })
            .chain(muliao_runtime::attachments::references(std::slice::from_ref(&meta)))
            .collect(),
    )];

    Mock::given(method("POST"))
        .and(path("/v1/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_string(message(
            serde_json::json!({"type": "tool_use", "id": "call_1", "name": "snap", "input": {"token": "t0p"}}),
            "tool_use",
            100,
            10,
        )))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    let resolved = HashMap::<String, Resolution>::new();
    let mut events = 0;
    let r = run.respond(&history, &mut |_| events += 1).await.unwrap();
    assert!(events > 0);
    history.push(Message::Assistant(r.clone()));

    // 发出的请求：指令在 system 里，附件展开成说明 + 图片内容，工具定义里只有 snap
    let sent: serde_json::Value = server.received_requests().await.unwrap()[0].body_json().unwrap();
    let body = sent.to_string();
    assert!(
        sent["system"][0]["text"]
            .as_str()
            .unwrap()
            .starts_with("## Runtime context\n- You are agent \"me\": 测试 agent")
    );
    assert!(body.contains("全局记忆"));
    let described = format!(
        "[Attached file \"photo.png\" (image/png, {} bytes), copied into your workspace at uploads/{}/photo.png]",
        meta.size, meta.id
    );
    // body 是 JSON：引号转义后再找
    let described = serde_json::to_string(&described).unwrap();
    assert!(body.contains(described.trim_matches('"')), "{body}");
    assert!(body.contains("\"type\":\"image\""), "the image content is sent: {body}");
    assert!(!body.contains("/api/files/"), "file references never reach the model");
    assert_eq!(sent["tools"].as_array().unwrap().len(), 1);
    assert_eq!(
        w.usage.session_totals(&SessionId::new("s1")).await.unwrap().requests,
        1,
        "billed right after the response"
    );

    // 执行工具：图片存成引用，附件并进结果
    let earlier = &history[..history.len() - 1];
    // 已经停止：一个调用也不启动
    let stopped = run.run_tools(earlier, &r, &resolved, std::future::ready(()), &mut |_| {}).await.unwrap();
    assert!(matches!(&stopped, ToolsRun::Stopped(done) if done.is_empty()), "no tool runs once stopped");
    let ToolsRun::Done(ToolsDone::Results(results)) =
        run.run_tools(earlier, &r, &resolved, std::future::pending(), &mut |_| {}).await.unwrap()
    else {
        panic!()
    };
    let t = &results[0];
    let Content::File(f) = &t.content[1] else { panic!("{t:?}") };
    let FileSource::Url(url) = &f.source else { panic!("the image is stored, not kept inline") };
    assert!(url.starts_with("/api/files/") && url.ends_with("/snap.png"), "{url}");
    let meta: ResultFiles = serde_json::from_value(t.metadata.clone().unwrap()).unwrap();
    assert_eq!(meta.attachments[0].name, "report.pdf");
    history.push(Message::User(Request {
        parts: results.into_iter().map(RequestPart::ToolResult).collect(),
        timestamp: Utc::now(),
        metadata: None,
    }));

    // 第二次请求：工具结果里的图片换回内容
    Mock::given(method("POST"))
        .and(path("/v1/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_string(message(
            serde_json::json!({"type": "text", "text": "好了"}),
            "end_turn",
            200,
            5,
        )))
        .mount(&server)
        .await;
    let r = run.respond(&history, &mut |_| {}).await.unwrap();
    assert_eq!(r.text(), "好了");
    let second = server.received_requests().await.unwrap()[1].body_json::<serde_json::Value>().unwrap().to_string();
    assert!(!second.contains("/api/files/"), "{second}");
    assert_eq!(second.matches("\"type\":\"image\"").count(), 2, "the attachment and the tool's picture");
    history.push(Message::Assistant(r));

    let totals = w.usage.session_totals(&SessionId::new("s1")).await.unwrap();
    assert_eq!((totals.requests, totals.input_tokens, totals.output_tokens), (2, 300, 15));

    // 落库前脱敏：snap 声明了 token
    let mut saved = history.clone();
    w.runtime.toolbox().redaction().messages(&mut saved);
    let Message::Assistant(a) = &saved[1] else { panic!() };
    assert_eq!(a.tool_calls().next().unwrap().args, r#"{"token":"[redacted]"}"#);
    drop(run);

    // 压缩：无工具的一次生成，记为压缩
    let summary = w.runtime.summarize(&assets, &SessionId::new("s1"), "summarize", "the transcript").await.unwrap();
    assert_eq!(summary, "好了");
    assert_eq!(w.usage.session_totals(&SessionId::new("s1")).await.unwrap().requests, 3);
}

#[tokio::test]
async fn probe_reports_connection_failures_to_the_caller() {
    let server = MockServer::start().await;
    let w = world(&server).await;
    write(&w.root.join("platform/mcp/shared.json"), r#"{"transport": "stdio", "command": "srv"}"#);
    let registry = McpRegistry::new(Arc::new(AssetSource::new(
        Arc::new(Database::open(&w._dir.path().join("t.db"), Arc::new(|| {})).unwrap()),
        w.root.clone(),
        4000,
        Arc::new(|_: &muliao_platform::config::ModelRef| Ok(())),
        Arc::new(ToolTable::of(vec![])),
    )));
    let shared = registry.get("shared").await.unwrap();
    let e = w.mcp.probe(&shared, &Actor::User).await.unwrap_err();
    let msg = e.to_string();
    assert!(msg.starts_with("could not connect: MCP server \"shared\" failed"), "{msg}");
    assert!(
        !msg.contains("/nonexistent/wrapped"),
        "the platform's servers are not confined when the user probes: {msg}"
    );
    assert!(w.confined.lock().unwrap().is_empty());
}
