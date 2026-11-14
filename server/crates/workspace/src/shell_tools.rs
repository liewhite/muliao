//! run_shell（前台，或 background 作为后台任务）、skill_run、job_status、job_kill。后台任务属于发起它的会话（结局投回那个
//! 会话）。
//!
//! 前台命令是阻塞的（调用不返回、一直等）：必须给 timeout；后台任务与 agent 并发：没有 timeout，按 heartbeat_seconds 投运行
//! 中检查，由 agent 决定继续等还是 job_kill。

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use agenter::{Tool, ToolCtx, ToolError, ToolOutput};
use muliao_assets::secrets::SecretUse;
use muliao_assets::skills::{SkillScripts, SkillStore};
use muliao_platform::errors::DomainError;
use muliao_platform::fs::{real, resolve_within};
use muliao_platform::ids::{CallId, GrantId, JobId};
use muliao_platform::shell::LEFT_RUNNING_NOTE;
use muliao_platform::text::truncate;
use muliao_platform::tool_views::{ToolRender, ViewBlock};
use muliao_platform::tooling::{BuiltinTool, RunScope, ToolTraits, builtin};
use muliao_sessions::tools::check_interval;
use schemars::JsonSchema;
use serde::Deserialize;

use crate::commands::{AgentCommands, Options};
use crate::grants::{GRANTED_NOTE, ScriptCall, ScriptGrants};
use crate::jobs::{End, JobRunner, JobState};
use muliao_platform::errors::Error;

/// run_shell 与 skill_run 共用的依赖。
pub struct ShellTools {
    pub commands: Arc<AgentCommands>,
    pub jobs: Arc<JobRunner>,
    pub skills: Arc<SkillStore>,
    pub grants: Arc<ScriptGrants>,
    pub max_output_chars: usize,
}

pub fn tools(deps: Arc<ShellTools>) -> Vec<BuiltinTool> {
    // 输出上限是配置的值，写进说明：模型不知道它，长输出被截了才学会
    let description = format!(
        "{}{BACKGROUND_DESCRIPTION}",
        SHELL_DESCRIPTION.replace("{max_output}", &deps.max_output_chars.to_string()),
    )
    .replace("{no_progress}", &muliao_sessions::outside::work::no_progress_hint());
    vec![
        builtin(RunShell { deps: deps.clone(), description }, ToolTraits::write().render(shell_render())),
        builtin(SkillRun(deps.clone()), ToolTraits::write().render(skill_run_render())),
        builtin(JobStatusTool(deps.clone()), ToolTraits::read()),
        builtin(JobKillTool(deps), ToolTraits::write()),
    ]
}

const SHELL_DESCRIPTION: &str = "Run a shell command (bash) in your workspace and return its exit code and combined output. \
By default it runs in the foreground, which is blocking: you wait until it ends, so you must pass `timeout`, \
the seconds it may take (estimate it yourself, there is no upper limit: e.g. 30 for a quick query, 600 for a \
test suite). If it is still running then, it is killed and you get `killed after Ns timeout` with the output \
so far. It ends when bash exits: processes it leaves running with `&` are not killed, but their output after \
that is not shown (redirect it to a file if you need it). \
Only the first {max_output} characters of output are kept, so limit it yourself: pipe through `| head` or `| tail`, \
use `rg -m N`, `cut -c1-200`, and read only the part of a long log you need. \
Search with ripgrep, not grep/find: `rg -n PATTERN [PATH]` for contents (`-g '*.py'` filters files, `-C 3` \
adds context), `rg --files [-g GLOB]` to find files; it skips .git and ignored files such as node_modules. \
`pgrep -f` / `pkill -f` also match the shell running your command, since its command line contains the \
pattern (here and over ssh on a remote host), so `pkill -f` can kill that shell: bracket one character, e.g. \
`pgrep -f '[a]rb live'`, or match the process name exactly with `pgrep -x`.";

const BACKGROUND_DESCRIPTION: &str = " To keep working while a long command runs, pass `background: true` and no `timeout`: this is non-blocking. \
It returns a job id at once and the job runs alongside you with no time limit; its output goes to \
jobs/<id>.log in your workspace. While it runs you get a check-in event every `heartbeat_seconds` (default \
60; choose it from how long you expect the job to take, e.g. 300 for a half-hour build) with its run time and \
last output lines: then either keep waiting ({no_progress}) or stop it with \
`job_kill`; `job_status` shows it at any time. When it ends you get an event with its exit code and last \
output.";

const SKILL_RUN_DESCRIPTION: &str = "Run a script from one of your skills: `<skill directory>/<script>` with `args`, executed directly (no shell \
parsing; it needs a shebang line and the executable bit). It runs like run_shell (in your workspace, only \
able to write where your commands may write, same output limit, same `timeout` / `background` / \
`heartbeat_seconds` rules). `secrets` works as in run_shell and also takes secrets marked \
`allow_shell = false`, which run_shell, trigger prechecks and streams refuse: such a key is meant to be used \
only through a script that wraps the operation with its own checks, so use the skill's script for it \
instead of writing the command yourself. A script listed under `approval:` in the skill's SKILL.md \
frontmatter waits for the user to approve or deny this exact call before it runs: your turn ends once this \
response's tool calls finish, and the script's result (or the denial) arrives as this tool's result. A call \
the user has chosen to always allow runs without waiting when it is repeated exactly (same script, args and \
secrets, and the skill's files other than SKILL.md unchanged).";

// run_shell 结果首行的开头：工具写出、展示规则认出，只在这里定义。首行总是工具自己写的一行状态，命令的输出从第二行起
const EXIT_CODE: &str = "exit_code:";
const KILLED: &str = "killed after";
const STARTED: &str = "started background job";

/// 同 Python 的 `shlex.quote`：只含安全字符的原样，其余用单引号括起（单引号本身写成 `'"'"'`）。
pub fn shell_quote(s: &str) -> String {
    if s.is_empty() {
        return "''".to_owned();
    }
    let safe = |c: char| c.is_ascii_alphanumeric() || "@%+=:,./-_".contains(c);
    if s.chars().all(safe) { s.to_owned() } else { format!("'{}'", s.replace('\'', "'\"'\"'")) }
}

/// 同 Python 的 `shlex.join`。
pub fn shell_join<S: AsRef<str>>(parts: &[S]) -> String {
    parts.iter().map(|p| shell_quote(p.as_ref())).collect::<Vec<_>>().join(" ")
}

/// 结果的展示：前台首行是退出码或超时被杀，其余是输出；后台首行是启动了哪个任务。认不出就显示原文。
fn shell_result(text: &str) -> Option<Vec<ViewBlock>> {
    let (head, output) = text.split_once('\n').unwrap_or((text, ""));
    if let Some(code) = head.strip_prefix(EXIT_CODE) {
        let code = code.trim();
        return Some(vec![ViewBlock::Terminal {
            command: None,
            output: Some(output.to_owned()),
            status: Some(format!("exit {code}")),
            failed: code != "0",
        }]);
    }
    if head.starts_with(KILLED) {
        return Some(vec![ViewBlock::Terminal {
            command: None,
            output: Some(output.to_owned()),
            status: Some(head.to_owned()),
            failed: true,
        }]);
    }
    if head.starts_with(STARTED) {
        return Some(vec![ViewBlock::Terminal {
            command: None,
            output: None,
            status: Some(head.to_owned()),
            failed: false,
        }]);
    }
    None
}

fn shell_render() -> ToolRender {
    ToolRender::new()
        .call(|a: &ShellArgs| {
            Some(vec![ViewBlock::Terminal {
                command: Some(a.command.clone()),
                output: None,
                status: None,
                failed: false,
            }])
        })
        .result_text(shell_result)
}

fn skill_run_render() -> ToolRender {
    ToolRender::new()
        .call(|a: &SkillRunArgs| {
            let mut parts = vec![format!("{}/{}", a.skill, a.script)];
            parts.extend(a.args.iter().cloned());
            Some(vec![ViewBlock::Terminal {
                command: Some(shell_join(&parts)),
                output: None,
                status: None,
                failed: false,
            }])
        })
        .result_text(shell_result)
}

/// 一次执行的方式（run_shell 与 skill_run 的参数里共有的部分）。
struct Execution<'a> {
    command: String,
    timeout: Option<f64>,
    secrets: &'a [String],
    background: bool,
    heartbeat_seconds: Option<f64>,
    /// command 是 agent 自己写的还是封装好的 skill 脚本调用（决定能不能注入 allow_shell = false 的 secret）
    secret_use: SecretUse,
    /// 给了就在参数校验通过后先停下等用户批准（批准的是这一份），不去问一个参数就不对的调用
    approval_pin: Option<String>,
}

impl ShellTools {
    /// run_shell 与 skill_run 共用的执行：前台或后台、参数用错场合的报错都一样。
    async fn execute(&self, scope: &RunScope, e: Execution<'_>) -> Result<String, ToolError> {
        if !e.background {
            if e.heartbeat_seconds.is_some() {
                return Err(ToolError::Retry(
                    "heartbeat_seconds is only for background jobs; a foreground command uses `timeout`".into(),
                ));
            }
            let Some(timeout) = e.timeout else {
                return Err(ToolError::Retry(
                    "timeout is required for a foreground command: estimate how many seconds it needs (no upper \
                     limit), or run it with `background: true`"
                        .into(),
                ));
            };
            // 也拒绝 NaN
            if timeout.is_nan() || timeout <= 0.0 {
                return Err(ToolError::Retry("timeout must be greater than 0 seconds".into()));
            }
            if let Some(pin) = e.approval_pin {
                return Err(ToolError::NeedsApproval { pin });
            }
            return self.run_foreground(scope, &e.command, timeout, e.secrets, e.secret_use).await;
        }
        if e.timeout.is_some() {
            return Err(ToolError::Retry(
                "background jobs have no timeout; leave `timeout` out (set `heartbeat_seconds` for how often you want \
                 to hear about it, kill it with job_kill)"
                    .into(),
            ));
        }
        let check = check_interval(e.heartbeat_seconds).map_err(ToolError::Retry)?;
        if let Some(pin) = e.approval_pin {
            return Err(ToolError::NeedsApproval { pin });
        }
        let job = self.jobs.start(scope, &e.command, e.secrets, e.secret_use, check).await?;
        Ok(format!(
            "{STARTED} {}; output goes to {}.\nYou will get a check-in event every {} seconds while it runs and an \
             event when it finishes; keep working or end your reply.",
            job.id, job.log_path, check
        ))
    }

    async fn run_foreground(
        &self,
        scope: &RunScope,
        command: &str,
        timeout: f64,
        secrets: &[String],
        secret_use: SecretUse,
    ) -> Result<String, ToolError> {
        let agent_shell = self.commands.prepare(&scope.agent_id, secrets, secret_use).await?;
        let limit = Duration::try_from_secs_f64(timeout).unwrap_or(Duration::MAX);
        let run = agent_shell
            .run(command, limit, Options { merge_stderr: true, ..Options::default() })
            .await
            .map_err(Error::from)?;
        let status = match run.result.exit_code {
            Some(code) => format!("{EXIT_CODE} {code}"),
            None => format!("{KILLED} {timeout}s timeout"),
        };
        let mut output = truncate(&run.result.stdout, self.max_output_chars);
        if let Some(note) = run.refusal {
            // 紧接在系统报错之后：输出以换行结尾就不再空一行
            if !output.ends_with('\n') {
                output.push('\n');
            }
            output.push_str(&note);
        }
        if run.result.left_running {
            output.push_str(&format!("\n[{LEFT_RUNNING_NOTE}]"));
        }
        Ok(format!("{status}\n{output}"))
    }
}

// ---- run_shell ----

#[derive(Deserialize, JsonSchema)]
pub struct ShellArgs {
    /// The command line to run.
    command: String,
    /// Foreground only, and required there: seconds before the command is killed (> 0, no upper limit; estimate it
    /// yourself). Leave it out with `background: true`.
    #[serde(default)]
    timeout: Option<f64>,
    /// Vault keys you can use (your own, or allowed by your roles or your own allow rules) to expose to the command as
    /// environment variables of the same name.
    /// Secret values in the output are replaced by `[secret:KEY]`.
    #[serde(default)]
    secrets: Vec<String>,
    /// Run it as a background job instead of waiting for it.
    #[serde(default)]
    background: bool,
    /// Background only: seconds between check-in events while the job runs (> 0, default 60).
    #[serde(default)]
    heartbeat_seconds: Option<f64>,
}

pub struct RunShell {
    deps: Arc<ShellTools>,
    description: String,
}

impl Tool<RunScope> for RunShell {
    type Args = ShellArgs;

    fn name(&self) -> &str {
        "run_shell"
    }

    fn description(&self) -> &str {
        &self.description
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: ShellArgs) -> Result<ToolOutput, ToolError> {
        let e = Execution {
            command: a.command,
            timeout: a.timeout,
            secrets: &a.secrets,
            background: a.background,
            heartbeat_seconds: a.heartbeat_seconds,
            secret_use: SecretUse::AgentCommand,
            approval_pin: None,
        };
        Ok(agenter::tool::text(self.deps.execute(ctx.deps, e).await?))
    }
}

// ---- skill_run ----

#[derive(Deserialize, JsonSchema)]
pub struct SkillRunArgs {
    /// Name of a skill in your skill index.
    skill: String,
    /// Path of the script relative to the skill's directory, e.g. `scripts/deploy.py`.
    script: String,
    /// Arguments passed to the script as they are (no shell parsing).
    #[serde(default)]
    args: Vec<String>,
    /// As in run_shell: foreground only, and required there (seconds, no upper limit).
    #[serde(default)]
    timeout: Option<f64>,
    /// As in run_shell (vault keys you can use, exposed as environment variables of the same name, values in the
    /// output replaced by `[secret:KEY]`), and also keys marked `allow_shell = false`.
    #[serde(default)]
    secrets: Vec<String>,
    /// As in run_shell: run it as a background job instead of waiting for it.
    #[serde(default)]
    background: bool,
    /// As in run_shell: background only, seconds between check-in events (default 60).
    #[serde(default)]
    heartbeat_seconds: Option<f64>,
}

fn executable(path: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Ok(c) = std::ffi::CString::new(path.as_os_str().as_bytes()) else { return false };
    // SAFETY: access 只读这个以 NUL 结尾的路径
    unsafe { libc::access(c.as_ptr(), libc::X_OK) == 0 }
}

/// skill 目录里的脚本加参数，拼成一条不经 shell 解析的命令；脚本执行前要用户批准时还给出它相对目录的路径（解析后的，
/// 始终允许按它认脚本），不用批准为 None。脚本必须落在目录里（经符号链接也不行）。阻塞 IO。
fn script_command(
    scripts: &SkillScripts,
    skill: &str,
    script: &str,
    args: &[String],
) -> Result<(String, Option<String>), DomainError> {
    let directory = &scripts.directory;
    let path = resolve_within(directory, script).map_err(|_| {
        DomainError::invalid(format!(
            "script must be a path inside the skill's directory ({}), got {script:?}",
            directory.display(),
        ))
    })?;
    if !path.as_path().is_file() {
        return Err(DomainError::not_found(format!(
            "skill {skill:?} has no script {script:?} (looked for {})",
            path.as_path().display()
        )));
    }
    if !executable(path.as_path()) {
        return Err(DomainError::invalid(format!(
            "{} is not executable: a skill script needs a shebang line and the executable bit (chmod +x)",
            path.as_path().display()
        )));
    }
    let gated = if scripts.needs_approval(&path) {
        let root = real(directory)
            .map_err(|e| DomainError::invalid(format!("cannot resolve {}: {e}", directory.display())))?;
        let rel = path.as_path().strip_prefix(root.as_path()).expect("resolve_within keeps it inside");
        Some(rel.to_string_lossy().into_owned())
    } else {
        None
    };
    let mut parts = vec![path.as_path().to_string_lossy().into_owned()];
    parts.extend(args.iter().cloned());
    Ok((shell_join(&parts), gated))
}

pub struct SkillRun(Arc<ShellTools>);

impl Tool<RunScope> for SkillRun {
    type Args = SkillRunArgs;

    fn name(&self) -> &str {
        "skill_run"
    }

    fn description(&self) -> &str {
        SKILL_RUN_DESCRIPTION
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: SkillRunArgs) -> Result<ToolOutput, ToolError> {
        let deps = &self.0;
        let scope = ctx.deps;
        let found = deps.skills.scripts(&a.skill, &scope.agent_id).await?;
        let (command, gated) = {
            let (found, skill, script, args) = (found.clone(), a.skill.clone(), a.script.clone(), a.args.clone());
            tokio::task::spawn_blocking(move || -> Result<_, ToolError> {
                let (command, gated) = script_command(&found, &skill, &script, &args)?;
                // 要批准的脚本：按 skill 目录的指纹认是哪一份
                let gated = match gated {
                    Some(script) => Some((script, found.fingerprint().map_err(|e| ToolError::Abort(Box::new(e)))?)),
                    None => None,
                };
                Ok((command, gated))
            })
            .await
            .expect("locating the script panicked")?
        };
        let clearance = match gated {
            None => Clearance::Free,
            Some((script, fingerprint)) => {
                let call = ScriptCall::new(&scope.agent_id, &a.skill, &script, &a.args, &a.secrets, &fingerprint);
                match ctx.approval {
                    // 批准后再执行时须与请求批准时的一样（批准的是那一份）；用户这次点了始终允许就记下
                    Some(approval) => {
                        if approval.pin != fingerprint {
                            return Err(ToolError::Retry(format!(
                                "the files of skill {:?} are not the version the user approved (they changed after \
                                 approval was requested, or the request did not record the version); nothing was run. \
                                 Call it again to ask the user to approve the current version.",
                                a.skill
                            )));
                        }
                        if approval.always {
                            deps.grants.grant(call, &scope.session_id, &CallId::new(ctx.call.id.clone())).await?;
                        }
                        Clearance::Approved
                    }
                    // 还没批准：有始终允许就照它，否则停下请用户批准
                    None => match deps.grants.find(&call).await? {
                        Some(grant) => Clearance::Granted(grant),
                        None => Clearance::Needed { pin: fingerprint },
                    },
                }
            }
        };
        let approval_pin = match &clearance {
            Clearance::Needed { pin } => Some(pin.clone()),
            Clearance::Free | Clearance::Granted(_) | Clearance::Approved => None,
        };
        let e = Execution {
            command,
            timeout: a.timeout,
            secrets: &a.secrets,
            background: a.background,
            heartbeat_seconds: a.heartbeat_seconds,
            secret_use: SecretUse::Wrapped,
            approval_pin,
        };
        let result = deps.execute(scope, e).await?;
        match clearance {
            Clearance::Granted(grant) => {
                deps.grants.used(grant).await?;
                Ok(agenter::tool::text(format!("{GRANTED_NOTE}\n{result}")))
            }
            Clearance::Free | Clearance::Needed { .. } | Clearance::Approved => Ok(agenter::tool::text(result)),
        }
    }
}

/// 一次 skill_run 的批准状态。
enum Clearance {
    /// 脚本不用批准
    Free,
    /// 要批准、还没批准：停下请用户批准，pin 是请求批准时 skill 目录的指纹
    Needed { pin: String },
    /// 用户「始终允许」过同样的调用（那条记录的 id）
    Granted(GrantId),
    /// 用户批准了这次调用
    Approved,
}

// ---- job_status / job_kill ----

#[derive(Deserialize, JsonSchema)]
pub struct JobArgs {
    /// The id `run_shell(background: true)` returned.
    job_id: i64,
}

pub struct JobStatusTool(Arc<ShellTools>);

impl Tool<RunScope> for JobStatusTool {
    type Args = JobArgs;

    fn name(&self) -> &str {
        "job_status"
    }

    fn description(&self) -> &str {
        "Show a background job's status (running, exited with its code, killed, interrupted), how long it has \
         run and the last lines of its output (read its log file for all of it). Use it to look at a job between \
         check-ins; it does not change anything."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: JobArgs) -> Result<ToolOutput, ToolError> {
        let info = self.0.jobs.info(JobId::new(a.job_id), &ctx.deps.agent_id).await?;
        let state = match info.state {
            JobState::Ended { end: End::Exited { code }, .. } => {
                format!("exited with code {}", code.map_or("None".to_owned(), |c| c.to_string()))
            }
            other => other.status().to_string(),
        };
        let tail = if info.tail.is_empty() { "(no output yet)" } else { &info.tail };
        Ok(agenter::tool::text(format!(
            "job {}: {state} ({})\ncommand: {}\nlog: {}\nlast lines:\n{tail}",
            info.id, info.running_for, info.command, info.log_path
        )))
    }
}

pub struct JobKillTool(Arc<ShellTools>);

impl Tool<RunScope> for JobKillTool {
    type Args = JobArgs;

    fn name(&self) -> &str {
        "job_kill"
    }

    fn description(&self) -> &str {
        "Kill a running background job (its whole process tree), e.g. when a check-in shows it is stuck or no \
         longer needed. Its final status (killed) arrives as an event."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: JobArgs) -> Result<ToolOutput, ToolError> {
        self.0.jobs.kill(JobId::new(a.job_id), &ctx.deps.agent_id).await?;
        Ok(agenter::tool::text(format!(
            "sent kill signal to job {}; its final status will arrive as an event",
            a.job_id
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_join_quotes_like_shlex() {
        assert_eq!(shell_join(&["/a b/run.sh", "x", "it's", "", "a=b,c"]), "'/a b/run.sh' x 'it'\"'\"'s' '' a=b,c");
    }

    #[test]
    fn results_render_as_terminals() {
        assert_eq!(
            shell_result("exit_code: 1\nboom"),
            Some(vec![ViewBlock::Terminal {
                command: None,
                output: Some("boom".into()),
                status: Some("exit 1".into()),
                failed: true
            }])
        );
        assert!(shell_result("something else").is_none());
    }
}
