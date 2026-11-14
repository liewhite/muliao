//! HTTP 层看到的东西：各领域给界面用的门面（[`Services`]，由组合根装好交进来）与 HTTP 自己的状态（登录、退避、前端构建）。
//! 接口只经这些调用领域，拿不到领域内部的对象。

use std::path::PathBuf;
use std::sync::Arc;

use muliao_assets::catalog::AssetCatalog;
use muliao_assets::distribution::Distribution;
use muliao_assets::lifecycle::AgentLifecycle;
use muliao_assets::mcp::McpRegistry;
use muliao_assets::memory::MemoryStore;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::AgentService;
use muliao_assets::skills::SkillStore;
use muliao_files::FileStore;
use muliao_history::History;
use muliao_issues::Board;
use muliao_notifications::Notifications;
use muliao_platform::bus::Bus;
use muliao_runtime::mcp::McpConnector;
use muliao_runtime::models::ModelRegistry;
use muliao_sessions::service::SessionService;
use muliao_speech::Transcriber;
use muliao_triggers::TriggerService;
use muliao_usage::UsageLog;
use muliao_workspace::browse::WorkspaceFiles;
use muliao_workspace::sites::Sites;
use muliao_workspace::{JobRunner, ScriptGrants};

use crate::auth::{Auth, LoginThrottle};

/// 各领域给界面用的门面。
pub struct Services {
    /// SSE：进程内的展示事件（只订阅）
    pub bus: Arc<Bus>,
    pub agents: Arc<AgentService>,
    /// 删除与归档
    pub lifecycle: Arc<AgentLifecycle>,
    /// 角色、agent 的规则与能用的、预览
    pub distribution: Arc<Distribution>,
    /// 资源池：全部资源、删除、转移 owner
    pub catalog: Arc<AssetCatalog>,
    pub memory: Arc<MemoryStore>,
    pub skills: Arc<SkillStore>,
    pub mcp: Arc<McpRegistry>,
    /// 连 MCP server（新增时的验证、测试连接）
    pub mcp_connector: Arc<McpConnector>,
    pub secrets: Arc<Secrets>,
    pub models: Arc<ModelRegistry>,
    pub sessions: SessionService,
    pub notifications: Notifications,
    pub board: Board,
    pub triggers: TriggerService,
    pub files: Arc<FileStore>,
    pub usage: Arc<UsageLog>,
    pub history: History,
    /// 会话页等待项里的后台任务：查看与结束
    pub jobs: Arc<JobRunner>,
    /// 设置页的「始终允许」：查看与撤销
    pub grants: Arc<ScriptGrants>,
    /// agent 页的「文件」：工作目录的只读浏览
    pub workspace_files: Arc<WorkspaceFiles>,
    /// agent 在工作目录 site/ 下写的网页
    pub sites: Arc<Sites>,
    /// 语音转文字；没配置 [transcription] 时为 None，界面不显示语音输入
    pub transcriber: Option<Arc<Transcriber>>,
}

/// HTTP 层自己的配置。
pub struct HttpConfig {
    /// 登录密码与 cookie 签名密钥（config 的 auth 节）
    pub password: String,
    pub secret_key: String,
    /// 对外地址：https 时 cookie 带 Secure
    pub public_url: String,
    /// 前端构建目录（index.html 与 assets/）；没有构建时只提供接口
    pub web_dir: PathBuf,
}

/// 处理请求时的全部状态（廉价克隆）。
#[derive(Clone)]
pub struct AppState(Arc<Inner>);

pub struct Inner {
    pub services: Services,
    pub auth: Auth,
    pub throttle: LoginThrottle,
    pub secure_cookie: bool,
    pub web_dir: PathBuf,
    /// 前端构建的版本（index.html 的摘要）；没有构建时为 None
    pub ui_version: Option<String>,
}

impl std::ops::Deref for AppState {
    type Target = Inner;
    fn deref(&self) -> &Inner {
        &self.0
    }
}

impl AppState {
    pub fn new(services: Services, config: HttpConfig) -> Self {
        Self(Arc::new(Inner {
            services,
            auth: Auth::new(&config.secret_key, &config.password),
            throttle: LoginThrottle::default(),
            secure_cookie: config.public_url.starts_with("https://"),
            ui_version: crate::web::ui_version(&config.web_dir),
            web_dir: config.web_dir,
        }))
    }
}
