//! 以 agent 身份跑命令（#618）：run_shell 前台与后台任务、skill_run、触发器的前置检查与 stream 都经这里起 agent 的命令。
//!
//! 一条命令以谁的身份跑由三件事一起决定，只在这里拼：注入哪些 secret（`Secrets::resolve`，shell 决定能不能注入
//! allow_shell = false 的）、在哪个目录（agent 的工作目录）、关在什么写沙箱里（[`Confiner::of`]）。命令失败时被沙箱拒绝的
//! 写补什么说明也只在这里定（[`AgentShell::refusal`]，按同一份沙箱）：跑到结束的命令随结果给出（[`CommandResult::refusal`]），
//! 起了不等的由调用方在它结束时拿退出码与输出来问；调用方只负责把说明拼在自己的报错后面。怎么起进程、读输出、杀进程组是
//! 平台层 `shell` 的事，这里不管。
//!
//! MCP 的 stdio server 不走这里：它不是 bash，secret 按使用者解析、沙箱按使用者或 owner 取，组合根把 [`confine`] 交给
//! 运行端口。

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use futures::FutureExt;
use muliao_assets::access::Actor;
use muliao_assets::error::Result as AssetsResult;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::workspace_of;
use muliao_platform::sandbox::Confinement;
use muliao_platform::shell::{Command, ShellResult, Spawned, run_bash, spawn_bash};

use crate::writable::{Confiner, denial_note};

/// [`AgentShell::run`] 的结果：命令的结果，加上被写沙箱拦下的写的说明（[`AgentShell::refusal`]），没有则 None。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandResult {
    pub result: ShellResult,
    pub refusal: Option<String>,
}

/// 起命令的选项（默认：不给 stdin、stderr 单独一路、只看最后一步的退出码）。
#[derive(Debug, Clone, Default)]
pub struct Options {
    /// 给命令的标准输入（跑到结束的命令）；None 接 /dev/null
    pub stdin: Option<Vec<u8>>,
    pub merge_stderr: bool,
    pub pipefail: bool,
}

/// 一个 agent 起命令的环境（[`AgentCommands::prepare`] 给出）：工作目录、注入的 secret 明文（调用方脱敏自己记下的输出
/// 时用）、写沙箱。起命令前现算，改了授权下一条命令就生效。
pub struct AgentShell {
    pub cwd: PathBuf,
    pub secrets: BTreeMap<String, String>,
    confinement: Confinement,
}

impl AgentShell {
    fn command<'a>(&'a self, script: &'a str, options: &Options) -> Command<'a> {
        Command {
            script,
            cwd: self.cwd.clone(),
            secrets: &self.secrets,
            confinement: &self.confinement,
            merge_stderr: options.merge_stderr,
            pipefail: options.pipefail,
        }
    }

    /// 起命令、不等它结束（后台任务、stream）：进程组与输出由调用方处置。stdin：给它一个可写的标准输入。
    pub fn spawn(&self, script: &str, stdin: bool, options: &Options) -> std::io::Result<Spawned> {
        spawn_bash(&self.command(script, options), stdin)
    }

    /// 跑到结束（前台 run_shell、前置检查），输出里的 secret 已替换，失败时带上沙箱拒绝的说明（看报错的那路输出：
    /// merge_stderr 时是 stdout，否则 stderr）。
    pub async fn run(&self, script: &str, timeout: Duration, options: Options) -> std::io::Result<CommandResult> {
        let result = run_bash(&self.command(script, &options), timeout, options.stdin.clone()).await?;
        let errors = if options.merge_stderr { &result.stdout } else { &result.stderr };
        let refusal = self.refusal(result.exit_code, errors);
        Ok(CommandResult { result, refusal })
    }

    /// 命令结束后补在报错后面的说明：没成功（退出码非 0，或超时、被杀没有退出码）且输出（报错的那部分）里有被写沙箱拦下
    /// 的写时给出，否则 None。退出码 0 不看：命令成功了，输出里引用的「Operation not permitted」不是它失败的原因。spawn
    /// 起的命令（后台任务、stream）由调用方在它结束时来问。
    pub fn refusal(&self, exit_code: Option<i32>, output: &str) -> Option<String> {
        if exit_code == Some(0) { None } else { denial_note(&self.confinement, output) }
    }
}

pub struct AgentCommands {
    secrets: Arc<Secrets>,
    workspaces_dir: PathBuf,
    confiner: Arc<Confiner>,
}

impl AgentCommands {
    pub fn new(secrets: Arc<Secrets>, workspaces_dir: PathBuf, confiner: Arc<Confiner>) -> Self {
        Self { secrets, workspaces_dir, confiner }
    }

    /// agent 的工作目录（与起命令的 cwd 同源；不起命令的读路径，如后台任务的日志，也按它）。
    pub fn workspace(&self, agent_id: &str) -> PathBuf {
        workspace_of(&self.workspaces_dir, agent_id)
    }

    /// agent 起命令的环境。shell：命令是 agent 自己写的（run_shell、触发器），不是 skill_run 拼出的脚本调用。secret 不存在
    /// 或不能交给这条命令时报领域错误，什么都还没起。
    pub async fn prepare(&self, agent_id: &str, secret_keys: &[String], shell: bool) -> AssetsResult<AgentShell> {
        let secrets = self.secrets.resolve(&Actor::Agent(agent_id.to_owned()), secret_keys, shell).await?;
        Ok(AgentShell { cwd: self.workspace(agent_id), secrets, confinement: self.confiner.of(agent_id).await? })
    }
}

/// 运行端口起 MCP stdio server 时用的沙箱（按 agent 现在的授权现算）。
pub fn confine(confiner: Arc<Confiner>) -> muliao_runtime::mcp::Confine {
    Arc::new(move |agent: String| {
        let confiner = confiner.clone();
        async move { confiner.of(&agent).await.map_err(|e| Box::new(e) as muliao_runtime::mcp::BoxError) }.boxed()
    })
}
