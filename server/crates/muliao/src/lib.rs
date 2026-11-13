//! 组合根：唯一一处创建并连接所有服务。只装配各领域给出的部件（工具、订阅、删除钩子、常驻循环），没有业务规则。
//!
//! [`assemble`] 建好全部对象；[`App::start`] 做对外服务之前必须做的（续删、收尾上次的后台任务、续跑会话、起触发器）；
//! [`App::router`] 是 HTTP 路由；[`App::shutdown`] 在 HTTP 排空之后停掉常驻的东西。

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::Context;
use muliao_assets::catalog::AssetCatalog;
use muliao_assets::distribution::Distribution;
use muliao_assets::lifecycle::AgentLifecycle;
use muliao_assets::mcp::McpRegistry;
use muliao_assets::memory::MemoryStore;
use muliao_assets::rules::ToolTable;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::AgentService;
use muliao_assets::skills::SkillStore;
use muliao_assets::source::AssetSource;
use muliao_files::FileStore;
use muliao_history::History;
use muliao_http::{AppState, HttpConfig, Services};
use muliao_issues::Board;
use muliao_notifications::{Notifications, Push, VapidKeys};
use muliao_platform::bus::Bus;
use muliao_platform::config::Settings;
use muliao_platform::db::{Database, migrate};
use muliao_platform::retry::Retry;
use muliao_platform::vault::Vault;
use muliao_runtime::mcp::McpConnector;
use muliao_runtime::models::ModelRegistry;
use muliao_runtime::toolbox::Toolbox;
use muliao_runtime::{Runtime, RuntimeDeps};
use muliao_sessions::actor::{Actors, Waker};
use muliao_sessions::compaction::Compactor;
use muliao_sessions::events::{Events, Subscribers};
use muliao_sessions::inbound::SenderInbox;
use muliao_sessions::live::Live;
use muliao_sessions::origins::{Origin, OriginNotes};
use muliao_sessions::run::Core;
use muliao_sessions::service::SessionService;
use muliao_sessions::tools::Stopper;
use muliao_sessions::work::SessionWork;
use muliao_triggers::{TriggerDeps, TriggerDomain};
use muliao_usage::UsageLog;
use muliao_workspace::{ScriptGrants, Workspace, WorkspaceDeps};

/// 每个 SSE 连接最多积压的事件数，再多就断开它（前端重连后重新拉取）
const BUS_BUFFER: usize = 1024;

/// 所有表的迁移（各领域一串，按领域记版本）
const MIGRATIONS: [muliao_platform::db::Migration; 9] = [
    muliao_assets::agents::MIGRATION,
    muliao_files::MIGRATION,
    muliao_usage::MIGRATION,
    muliao_sessions::MIGRATION,
    muliao_workspace::MIGRATION,
    muliao_notifications::MIGRATION,
    muliao_issues::MIGRATION,
    muliao_triggers::MIGRATION,
    muliao_history::MIGRATION,
];

/// 装配好的服务。
pub struct App {
    pub bus: Arc<Bus>,
    state: AppState,
    source: Arc<AssetSource>,
    lifecycle: Arc<AgentLifecycle>,
    workspace: Workspace,
    triggers: TriggerDomain,
    actors: Actors,
}

/// 建好全部对象（不对外服务、不跑常驻的东西，之后调 [`App::start`]）。必须在 tokio 运行时里调用。web_dir：前端构建目录。
pub async fn assemble(settings: Settings, web_dir: PathBuf) -> anyhow::Result<App> {
    let tz = settings.timezone;
    let rt = &settings.runtime;
    std::fs::create_dir_all(&settings.data_dir).with_context(|| format!("creating {}", settings.data_dir.display()))?;
    let db_path = settings.db_path();
    migrate(&db_path, &MIGRATIONS).context("migrating the database")?;
    let bus = Arc::new(Bus::new(BUS_BUFFER));
    // 写过表的事务提交后：前端正在显示的页面重新拉取（不分哪类数据）
    let changed = bus.clone();
    let db = Arc::new(Database::open(&db_path, Arc::new(move || changed.changed())).context("opening the database")?);
    // 模型、网页、语音、推送共用的出站客户端（走系统代理）
    let client = muliao_platform::net::client().context("building the HTTP client")?;

    // ---- 资源目录、agent、分发 ----
    let models = Arc::new(ModelRegistry::new(&settings.llm, client.clone()));
    // 工具表：资源快照要用，工具又要用资源目录构造，所以先建待填的表、装好工具箱后填
    let (table, binder) = ToolTable::pending();
    let source = Arc::new(AssetSource::new(
        db.clone(),
        settings.assets_dir.clone(),
        rt.memory_max_chars,
        models.model_check(),
        table.clone(),
    ));
    let vault = Arc::new(Vault::new(settings.vault.key.expose()).context("the vault key")?);
    let secrets = Arc::new(Secrets::new(source.clone(), vault));
    let agents =
        Arc::new(AgentService::new(source.clone(), settings.workspaces_dir(), settings.llm.default_model.clone()));
    let distribution = Arc::new(Distribution::new(source.clone()));
    let memory = Arc::new(MemoryStore::new(source.clone()));
    let skills = Arc::new(SkillStore::new(source.clone()));
    let mcp = Arc::new(McpRegistry::new(source.clone()));

    // ---- 小领域 ----
    let files =
        Arc::new(FileStore::open(db.clone(), settings.files_dir(), rt.upload_max_bytes).context("file storage")?);
    let usage = Arc::new(UsageLog::new(db.clone(), tz));
    let history = History::new(db.clone(), tz);
    let web = settings.web.as_ref().map(|c| muliao_web::build(c, client.clone()));
    let transcriber = settings.transcription.as_ref().map(|c| muliao_speech::build(c, client.clone()));

    // ---- 会话领域先给出的端口（别的领域与工具要用） ----
    let (waker, wakes) = Waker::channel();
    let stopper = Stopper::default();
    let events = Arc::new(Events::default());

    // ---- 工作区 ----
    let home = std::env::home_dir().context("the service user has no home directory")?;
    let sandbox = muliao_platform::sandbox::sandbox_for(&home).map_err(anyhow::Error::msg)?;
    let workspace = Workspace::build(WorkspaceDeps {
        db: db.clone(),
        sandbox,
        workspaces_dir: settings.workspaces_dir(),
        home,
        private_files: settings.private_files(),
        distribution: distribution.clone(),
        secrets: secrets.clone(),
        assets: source.clone(),
        skills: skills.clone(),
        files: files.clone(),
        work: SessionWork::new(waker.clone()),
        max_output_chars: rt.tool_output_max_chars,
    });
    // MCP 的 stdio server 与 agent 的命令关在同一个写沙箱里
    let mcp_connector =
        Arc::new(McpConnector::new(secrets.clone(), muliao_workspace::commands::confine(workspace.confiner.clone())));

    // ---- 通知 ----
    let keys = VapidKeys::load_or_create(&settings.vapid_key_path()).context("the VAPID key")?;
    let push =
        Arc::new(Push::new(db.clone(), keys, settings.push.vapid_subject.clone(), client.clone(), Retry::default()));
    let notifications = Notifications::new(db.clone(), bus.clone(), push);

    // ---- issue 看板 ----
    let board = Board::new(db.clone(), SenderInbox::new(waker.clone()), notifications.clone(), stopper.clone(), tz);

    // ---- 触发器 ----
    let triggers = TriggerDomain::build(TriggerDeps {
        db: db.clone(),
        tz,
        inbox: SenderInbox::new(waker.clone()),
        notifications: notifications.clone(),
        commands: workspace.commands.clone(),
        precheck_timeout_seconds: rt.precheck_timeout_seconds,
    });

    // ---- agent 的删除与归档：各领域登记的清理与检查 ----
    let lifecycle = Arc::new(AgentLifecycle::new(
        source.clone(),
        settings.workspaces_dir(),
        vec![
            muliao_sessions::deletion::deletion_hook(waker.clone(), events.clone()),
            ScriptGrants::deletion_hook(),
            board.deletion_hook(),
            triggers.service.deletion_hook(),
        ],
        vec![muliao_sessions::deletion::archive_blocker()],
    ));

    // ---- 工具箱：各领域的工具；一次运行开放哪些只按分发规则算 ----
    let max_chars = rt.tool_output_max_chars;
    let mut tools = muliao_sessions::tools::tools(db.clone(), waker.clone(), stopper.clone(), tz);
    tools.extend(muliao_assets::tools::tools(agents.clone(), distribution.clone(), lifecycle.clone(), secrets.clone()));
    tools.extend(workspace.tools());
    tools.extend(muliao_notifications::tools::tools(notifications.clone()));
    tools.extend(muliao_issues::tools::tools(board.clone()));
    tools.extend(triggers.tools());
    tools.extend(muliao_history::tools(history.clone(), max_chars));
    // 「配置了哪些可选能力」只在这里判断一次：没配置 [web] 就没有网页工具
    if let Some(web) = &web {
        tools.extend(muliao_web::tools::tools(web.clone(), max_chars));
    }
    let toolbox = Arc::new(Toolbox::new(tools));
    binder.bind(toolbox.specs());

    // ---- 运行端口与会话 actor ----
    let runtime = Arc::new(Runtime::new(RuntimeDeps {
        models: models.clone(),
        toolbox: toolbox.clone(),
        mcp: mcp_connector.clone(),
        source: source.clone(),
        agents: agents.clone(),
        files: files.clone(),
        usage: usage.clone(),
        tz,
    }));
    // 跨领域的反应：会话事件的订阅者一次登记齐
    let mut subscribers = Subscribers::default();
    notifications.subscribe(&mut subscribers);
    board.subscribe(&mut subscribers);
    workspace.subscribe(&mut subscribers);
    events.subscribe(subscribers);
    let live = Live::new(bus.clone());
    // 会话领域自己的来源说明由它自己合进来；这里给别的领域开出的会话的说明
    let notes = OriginNotes::new([
        TriggerDomain::origin_note(),
        (Origin::Issue, muliao_issues::rules::SESSION_NOTE.to_owned()),
    ]);
    let threshold = rt.compaction_threshold_tokens;
    let core = Arc::new(Core {
        db: db.clone(),
        runtime: runtime.clone(),
        files: files.clone(),
        agents: agents.clone(),
        live: live.clone(),
        events: events.clone(),
        notes,
        waker: waker.clone(),
        compactor: Compactor {
            db: db.clone(),
            runtime,
            threshold_tokens: threshold,
            keep_steps: rt.compaction_keep_steps as usize,
        },
        renders: toolbox.renders(),
        redaction: toolbox.redaction(),
        tz,
        bus: bus.clone(),
    });
    let actors = Actors::start(core, wakes);
    stopper.bind(actors.clone());
    let sessions = SessionService::new(db.clone(), waker, actors.clone(), events, live, toolbox.renders(), threshold);

    let services = Services {
        bus: bus.clone(),
        agents: agents.clone(),
        lifecycle: lifecycle.clone(),
        distribution,
        catalog: Arc::new(AssetCatalog::new(source.clone())),
        memory,
        skills,
        mcp,
        mcp_connector,
        secrets,
        models,
        sessions,
        notifications,
        board,
        triggers: triggers.service.clone(),
        files,
        usage,
        history,
        jobs: workspace.jobs.clone(),
        grants: workspace.grants.clone(),
        workspace_files: Arc::new(workspace.browse.clone()),
        sites: Arc::new(workspace.sites.clone()),
        transcriber,
    };
    let state = AppState::new(
        services,
        HttpConfig {
            password: settings.auth.password.expose().to_owned(),
            secret_key: settings.auth.secret_key.expose().to_owned(),
            public_url: settings.server.public_url.clone(),
            web_dir,
        },
    );
    Ok(App { bus, state, source, lifecycle, workspace, triggers, actors })
}

impl App {
    /// 对外服务之前：确保全局记忆在、接着删上次没删完的 agent、收掉上次没收尾的后台任务并起运行中
    /// 检查、接着跑停在半路或有信没处理的会话、起触发器、起资源目录的监听。
    pub async fn start(&self) -> anyhow::Result<()> {
        self.source.ensure_global_memory().await.context("the global memory")?;
        self.lifecycle.resume_deletions().await.context("resuming agent deletions")?;
        self.workspace.jobs.run().await.context("starting background jobs")?;
        self.actors.resume().await.context("resuming sessions")?;
        self.triggers.start();
        let (source, bus) = (self.source.clone(), self.bus.clone());
        tokio::spawn(async move {
            if let Err(e) = source.watch(move || bus.changed()).await {
                tracing::error!(error = %e, "watching the asset tree failed: the UI will not refresh on file changes");
            }
        });
        Ok(())
    }

    pub fn router(&self) -> axum::Router {
        muliao_http::router(self.state.clone())
    }

    /// HTTP 排空之后：停掉触发器的常驻循环与流式命令，再结束后台任务的进程（记为被中断，下次启动时交回会话）。正在跑的
    /// 会话运行随进程结束，下次启动时从上一步续跑。
    pub async fn shutdown(&self) {
        self.triggers.shutdown().await;
        self.workspace.jobs.shutdown().await;
    }
}
