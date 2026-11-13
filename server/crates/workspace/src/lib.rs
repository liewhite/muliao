//! 工作区（docs/architecture.md 第 9 节）：agent 在自己工作目录里用的工具——文件工具（read/write/edit_file、share_file）、
//! run_shell（前台与后台）、skill_run（含等批准与「始终允许」）、job_status / job_kill——以及后台任务（jobs 表：启动、照看、
//! 结束、运行中检查）、写沙箱的判断（[`writable`]）、以 agent 身份起命令（[`commands`]，触发器也用）、界面对工作目录的只读
//! 浏览（[`browse`]）与 agent 页面（[`sites`]）。
//!
//! 组合根按这个顺序接上：[`Workspace::build`] → [`Workspace::tools`] 进工具箱 → [`Workspace::subscribe`] 登记会话事件 →
//! [`ScriptGrants::deletion_hook`] 交给 agent 的删除流程 → 对外服务之前 `jobs.run()`（先收掉上次没收尾的任务，再起运行中检查）→
//! 关停时 `jobs.shutdown()`。MCP 的沙箱用 [`commands::confine`]。

pub mod browse;
pub mod commands;
pub mod edits;
pub mod file_tools;
pub mod grants;
pub mod jobs;
pub mod output;
pub mod shell_tools;
pub mod sites;
pub mod text;
pub mod writable;

use std::path::PathBuf;
use std::sync::Arc;

use muliao_assets::distribution::Distribution;
use muliao_assets::secrets::Secrets;
use muliao_assets::skills::SkillStore;
use muliao_assets::source::AssetSource;
use muliao_files::FileStore;
use muliao_platform::db::{Database, Migration};
use muliao_platform::sandbox::Sandbox;
use muliao_platform::tooling::BuiltinTool;
use muliao_sessions::events::Subscribers;
use muliao_sessions::work::SessionWork;

pub use grants::ScriptGrants;
pub use jobs::JobRunner;

pub const MIGRATION: Migration = Migration {
    domain: "workspace",
    steps: &[
        include_str!("../migrations/001_workspace.sql"),
        include_str!("../migrations/002_grant_keys.sql"),
        include_str!("../migrations/003_jobs_columns.sql"),
    ],
};

/// 工作区领域要的外部依赖。
pub struct WorkspaceDeps {
    pub db: Arc<Database>,
    pub sandbox: Arc<dyn Sandbox>,
    pub workspaces_dir: PathBuf,
    /// 服务运行用户的家目录（与写沙箱适配器的是同一个）
    pub home: PathBuf,
    /// 平台的私有文件（配置的 private_files）：只有 admin 能读写
    pub private_files: Vec<PathBuf>,
    pub distribution: Arc<Distribution>,
    pub secrets: Arc<Secrets>,
    pub assets: Arc<AssetSource>,
    pub skills: Arc<SkillStore>,
    pub files: Arc<FileStore>,
    pub work: SessionWork,
    /// 工具输出交给模型的上限（runtime.tool_output_max_chars）
    pub max_output_chars: usize,
}

pub struct Workspace {
    pub confiner: Arc<writable::Confiner>,
    pub commands: Arc<commands::AgentCommands>,
    pub jobs: Arc<JobRunner>,
    pub grants: Arc<ScriptGrants>,
    pub browse: browse::WorkspaceFiles,
    pub sites: sites::Sites,
    tools: Vec<BuiltinTool>,
}

impl Workspace {
    pub fn build(d: WorkspaceDeps) -> Self {
        let confiner = Arc::new(writable::Confiner::new(
            d.sandbox,
            d.workspaces_dir.clone(),
            d.distribution,
            d.home,
            d.private_files,
        ));
        let commands = Arc::new(commands::AgentCommands::new(d.secrets, d.workspaces_dir.clone(), confiner.clone()));
        let jobs = JobRunner::new(d.db.clone(), commands.clone(), d.work);
        let grants = Arc::new(ScriptGrants::new(d.db));
        let mut tools = file_tools::tools(Arc::new(file_tools::FileTools {
            confiner: confiner.clone(),
            assets: d.assets,
            files: d.files,
            max_output_chars: d.max_output_chars,
        }));
        tools.extend(shell_tools::tools(Arc::new(shell_tools::ShellTools {
            commands: commands.clone(),
            jobs: jobs.clone(),
            skills: d.skills,
            grants: grants.clone(),
            max_output_chars: d.max_output_chars,
        })));
        Self {
            confiner,
            commands,
            jobs,
            grants,
            browse: browse::WorkspaceFiles::new(d.workspaces_dir.clone()),
            sites: sites::Sites::new(d.workspaces_dir),
            tools,
        }
    }

    /// 本领域的全部工具（组合根交给运行端口的工具箱）。
    pub fn tools(&self) -> Vec<BuiltinTool> {
        self.tools.clone()
    }

    /// 登记会话事件的订阅：会话被停止时结束被清掉的后台任务。
    pub fn subscribe(&self, subscribers: &mut Subscribers) {
        let jobs = self.jobs.clone();
        subscribers.on_session_stopped(move |tx, event| {
            jobs.on_stopped(tx, event);
            Ok(())
        });
    }
}
