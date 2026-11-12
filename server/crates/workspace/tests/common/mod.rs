//! 工作区的离线测试环境：真的库、资源目录、写沙箱（sandbox-exec，enforce），两个 agent 与一个会话。

#![allow(dead_code)]

use std::path::PathBuf;
use std::sync::Arc;

use agenter::{Approval, Content, ToolCall, ToolCtx, ToolError};
use chrono::Utc;
use muliao_assets::access::Actor;
use muliao_assets::distribution::Distribution;
use muliao_assets::rules::ToolTable;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::{AgentService, AgentSpec};
use muliao_assets::skills::SkillStore;
use muliao_assets::source::AssetSource;
use muliao_assets::tree::Owner;
use muliao_files::FileStore;
use muliao_platform::db::{Database, migrate};
use muliao_platform::sandbox::{Mode, sandbox_for};
use muliao_platform::tooling::{Audience, BuiltinTool, RunScope};
use muliao_platform::vault::Vault;
use muliao_runtime::toolbox::Toolbox;
use muliao_sessions::actor::Waker;
use muliao_sessions::events::{Events, Subscribers};
use muliao_sessions::mailbox::{Mail, queued_in};
use muliao_sessions::work::SessionWork;
use muliao_workspace::{Workspace, WorkspaceDeps};

pub struct World {
    pub dir: tempfile::TempDir,
    pub db: Arc<Database>,
    pub workspace: Workspace,
    pub source: Arc<AssetSource>,
    pub secrets: Arc<Secrets>,
    pub files: Arc<FileStore>,
    pub tools: Vec<BuiltinTool>,
    pub me: String,
    pub other: String,
    pub session: String,
    pub root: PathBuf,
    pub events: Arc<Events>,
    _wakes: tokio::sync::mpsc::UnboundedReceiver<String>,
}

/// 一次工具调用的结局，变成文字好比对：交还模型的错误照原文，等批准的写成 `NEEDS_APPROVAL <pin>`。
#[derive(Debug, Clone, PartialEq)]
pub enum Out {
    Ok(String),
    Retry(String),
    NeedsApproval(String),
    Other(String),
}

impl Out {
    pub fn text(&self) -> &str {
        match self {
            Out::Ok(t) | Out::Retry(t) | Out::NeedsApproval(t) | Out::Other(t) => t,
        }
    }

    pub fn ok(&self) -> &str {
        match self {
            Out::Ok(t) => t,
            other => panic!("expected a result, got {other:?}"),
        }
    }

    pub fn retry(&self) -> &str {
        match self {
            Out::Retry(t) => t,
            other => panic!("expected an error for the model, got {other:?}"),
        }
    }
}

pub async fn world(max_output_chars: usize) -> World {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("t.db");
    migrate(
        &db_path,
        &[
            muliao_assets::agents::MIGRATION,
            muliao_files::MIGRATION,
            muliao_sessions::MIGRATION,
            muliao_workspace::MIGRATION,
        ],
    )
    .unwrap();
    let db = Arc::new(Database::open(&db_path, Arc::new(|| {})).unwrap());
    let root = dir.path().join("assets");
    std::fs::create_dir_all(root.join("platform/memory")).unwrap();
    std::fs::write(root.join("platform/memory/global.md"), "全局记忆\n").unwrap();
    let table = Arc::new(ToolTable::new());
    let source = Arc::new(AssetSource::new(db.clone(), root.clone(), 4000, Arc::new(|_, _| Ok(())), table.clone()));
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
        private_files: vec![dir.path().join("config.toml")],
        distribution: Arc::new(Distribution::new(source.clone())),
        secrets: secrets.clone(),
        assets: source.clone(),
        skills: Arc::new(SkillStore::new(source.clone())),
        files: files.clone(),
        work: SessionWork::new(waker),
        max_output_chars,
    });
    let tools = workspace.tools();
    table.bind(Toolbox::new(tools.clone()).specs());
    let events = Arc::new(Events::default());
    let mut subscribers = Subscribers::default();
    workspace.subscribe(&mut subscribers);
    events.subscribe(subscribers);
    let spec = |name: &str| AgentSpec { name: name.into(), description: "测试".into(), ..Default::default() };
    let me = agents.create(spec("me"), &Actor::User).await.unwrap().id;
    let other = agents.create(spec("other"), &Actor::User).await.unwrap().id;
    std::fs::create_dir_all(agents.workspace(&me)).unwrap();
    let session = "s1".to_owned();
    let (sid, aid) = (session.clone(), me.clone());
    db.write(move |tx| {
        tx.execute(
            "INSERT INTO sessions (id, agent_id, title, origin, created_at, updated_at) VALUES (?1, ?2, 't', 'user', ?3, ?3)",
            rusqlite::params![sid, aid, Utc::now()],
        )
    })
    .await
    .unwrap();
    World { dir, db, workspace, source, secrets, files, tools, me, other, session, root, events, _wakes: wakes }
}

impl World {
    pub fn scope(&self) -> RunScope {
        RunScope::new(self.me.clone(), self.ws(), self.session.clone(), Audience::User)
    }

    pub fn ws(&self) -> PathBuf {
        self.dir.path().join("workspaces").join(&self.me)
    }

    /// 这个 agent 在资源目录里的目录（按名字）。
    pub fn asset_dir(&self, name: &str) -> PathBuf {
        self.root.join(name)
    }

    pub async fn call_in(&self, scope: &RunScope, name: &str, args: serde_json::Value, approval: Option<&Approval>) -> (Out, Vec<Content>) {
        let tool = self.tools.iter().find(|t| t.name() == name).unwrap_or_else(|| panic!("no tool {name}"));
        let args = args.to_string();
        let call = ToolCall { id: format!("call-{name}"), name: name.into(), args: args.clone(), meta: None };
        let ctx = ToolCtx { deps: scope, call: &call, retries: 0, approval };
        match tool.tool.call(ctx, &args).await {
            Ok(content) => {
                let text = content
                    .iter()
                    .filter_map(|c| match c {
                        Content::Text { text } => Some(text.clone()),
                        _ => None,
                    })
                    .collect::<Vec<_>>()
                    .join("");
                (Out::Ok(text), content)
            }
            Err(ToolError::Retry(m)) => (Out::Retry(m), Vec::new()),
            Err(ToolError::NeedsApproval { pin }) => (Out::NeedsApproval(pin), Vec::new()),
            Err(e) => (Out::Other(format!("{e:?}")), Vec::new()),
        }
    }

    pub async fn call(&self, name: &str, args: serde_json::Value) -> Out {
        self.call_in(&self.scope(), name, args, None).await.0
    }

    /// 依次调用，返回每次的文字（错误交还模型的原文）。
    pub async fn run(&self, calls: Vec<(&str, serde_json::Value)>) -> Vec<String> {
        let scope = self.scope();
        let mut out = Vec::new();
        for (name, args) in calls {
            out.push(self.call_in(&scope, name, args, None).await.0.text().to_owned());
        }
        out
    }

    pub async fn queued(&self) -> Vec<Mail> {
        let sid = self.session.clone();
        self.db.read(move |c| queued_in(c, &sid)).await.unwrap()
    }

    pub async fn set_secret(&self, key: &str, value: &str, allow_shell: bool) {
        self.secrets
            .set(key, value, "", &Actor::Agent(self.me.clone()), &Owner::Agent(self.me.clone()), Some(allow_shell))
            .await
            .unwrap();
    }
}
