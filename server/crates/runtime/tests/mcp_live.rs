//! MCP 连真的 stdio server（官方 Python SDK 的 FastMCP，用 uv 在 target/ 下建 venv，没有 `uv` 时跳过），关在真的写沙箱
//! （sandbox-exec）里：server 只能写使用者的可写路径；配置里的 `${vault:KEY}` 按使用者能用的 secret 代入；连接失败的信息里
//! secret 明文被换掉。

use muliao_platform::ids::AgentId;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use agenter::{Content, ToolCall, ToolCtx};
use futures::FutureExt;
use muliao_assets::access::Actor;
use muliao_assets::rules::ToolTable;
use muliao_assets::run_assets::RunAssets;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::{AgentService, AgentSpec};
use muliao_assets::source::AssetSource;
use muliao_assets::tree::Owner;
use muliao_platform::db::{Database, migrate};
use muliao_platform::sandbox::{Confinement, WritablePaths, sandbox_for};
use muliao_platform::vault::Vault;
use muliao_runtime::mcp::{Confine, McpConnector};

const SERVER: &str = r#"
import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("t")

@mcp.tool()
def write(path: str) -> str:
    """Write a file."""
    try:
        open(path, "w").write("x")
        return "written"
    except OSError as e:
        return f"failed: {e.strerror}"

@mcp.tool()
def env(name: str) -> str:
    """An environment variable."""
    import os
    return os.environ.get(name, "<unset>")

mcp.run(transport="stdio")
"#;

/// 装好 mcp 的 Python（只建一次）；没有 uv 时 None。
fn python() -> Option<PathBuf> {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/mcp-test-venv");
    let py = dir.join("bin/python");
    if !py.exists() {
        let run = |args: &[&str]| std::process::Command::new("uv").args(args).output();
        let made = run(&["venv", "--quiet", dir.to_str()?]).ok()?;
        if !made.status.success() {
            return None;
        }
        let installed = run(&["pip", "install", "--quiet", "--python", py.to_str()?, "mcp<2"]).ok()?;
        assert!(installed.status.success(), "{}", String::from_utf8_lossy(&installed.stderr));
    }
    Some(py)
}

fn write(path: &Path, text: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

#[tokio::test]
async fn stdio_server_runs_in_the_users_write_sandbox() {
    let Some(python) = python() else {
        eprintln!("skipped: uv is not installed");
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let base = muliao_platform::fs::real(dir.path()).unwrap().into_path_buf();
    let db_path = base.join("t.db");
    migrate(&db_path, &[muliao_assets::agents::MIGRATION]).unwrap();
    let db = Arc::new(Database::open(&db_path, Arc::new(|| {})).unwrap());
    let root = base.join("assets");
    std::fs::create_dir_all(root.join("platform/memory")).unwrap();
    let source =
        Arc::new(AssetSource::new(db, root.clone(), 4000, Arc::new(|_: &str| Ok(())), Arc::new(ToolTable::of(vec![]))));
    let agents = AgentService::new(source.clone(), base.join("workspaces"), "p:m".into());
    let me = agents
        .create(AgentSpec { name: "me".into(), description: "d".into(), ..Default::default() }, &Actor::User)
        .await
        .unwrap()
        .id;
    let vault = Arc::new(Vault::new("3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=").unwrap());
    let secrets = Arc::new(Secrets::new(source.clone(), vault));
    secrets.set("TOKEN", "hunter2-secret", "", &Actor::User, &Owner::Agent(me.clone()), None).await.unwrap();
    let script = base.join("server.py");
    std::fs::write(&script, SERVER).unwrap();
    write(
        &root.join("me/mcp/t.json"),
        &serde_json::json!({
            "transport": "stdio",
            "command": python,
            "args": [script],
            "env": {"TOKEN": "${vault:TOKEN}"},
        })
        .to_string(),
    );
    let workspace = agents.workspace(&me);
    let home = std::env::home_dir().unwrap();
    let sandbox = sandbox_for(&home).unwrap();
    let ws = workspace.clone();
    let confine: Confine = Arc::new(move |agent: AgentId| {
        let (sandbox, home, ws) = (sandbox.clone(), home.clone(), ws.clone());
        async move {
            Ok(Confinement {
                sandbox,
                writable: WritablePaths::of(vec![(true, ws)], agent.to_string()),
                home,
                explain: Arc::new(|_: &Path| String::new()),
                unreadable: vec![],
            })
        }
        .boxed()
    });
    let connector = McpConnector::new(secrets, confine);
    let servers = async || RunAssets::of(&source.snapshot().await.unwrap(), &me).unwrap().mcp;

    let reachable = connector.connect_for(&me, &servers().await, &workspace).await.unwrap();
    assert!(reachable.notices.is_empty(), "{:?}", reachable.notices);
    let tools = reachable.connections[0].tools::<()>();
    let call = |name: &str, args: serde_json::Value| {
        let t = tools.iter().find(|t| t.def().name == name).unwrap().clone();
        async move {
            let args = args.to_string();
            let c = ToolCall { id: "1".into(), name: t.def().name.clone(), args: args.clone(), meta: None };
            let out = t.call(ToolCtx { deps: &(), call: &c, retries: 0, approval: None }, &args).await.unwrap();
            match out.as_slice() {
                [Content::Text { text }] => text.clone(),
                other => panic!("{other:?}"),
            }
        }
    };
    assert_eq!(call("t_env", serde_json::json!({"name": "TOKEN"})).await, "hunter2-secret");
    let inside = workspace.join("ok.txt");
    assert_eq!(call("t_write", serde_json::json!({"path": inside})).await, "written");
    let outside = base.join("escaped.txt");
    let refused = call("t_write", serde_json::json!({"path": outside})).await;
    assert!(refused.starts_with("failed:"), "{refused}");
    assert!(!outside.exists());

    // 连不上时信息里的 secret 明文被换掉
    write(&root.join("me/mcp/bad.json"), r#"{"transport": "http", "url": "http://127.0.0.1:9/${vault:TOKEN}"}"#);
    let again = connector.connect_for(&me, &servers().await, &workspace).await.unwrap();
    let notice = again.notices.join("\n");
    assert!(notice.contains("MCP server \"bad\"") && notice.contains("127.0.0.1:9/[secret:TOKEN]"), "{notice}");
    assert!(!notice.contains("hunter2"), "{notice}");
}
