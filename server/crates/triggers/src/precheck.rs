//! 前置检查：触发前先跑一条便宜、确定性的命令，只有真有事才调用 agent（所有触发方式共用）。
//!
//! 约定：命令在该 agent 的工作目录里用 `bash -o pipefail` 执行，环境与 run_shell 相同，可按 key 注入 secret；stdin 是原始事件
//! 内容（流式命令的输出行，定时与手动触发为空）。退出码 0 时按 fire_when 决定（[`decide`]）：
//! - output（默认）：stdout 非空就触发，stdout 作为事件内容交给 agent（替换原始事件，可以借此过滤、裁剪）；为空不触发。
//! - changed：与上一次的输出逐字比较，变了才触发，交给 agent 的是差异加完整输出；第一次只记基线、不触发。
//!
//! 非 0 退出、超时、输出过大、secret 不可用：检查本身失败（[`Failed`]），不触发，原因记成触发器的出错（[`crate::faults`]）。
//! 不当作「没事」跳过——那会把脚本的故障变成静默的漏触发。
//!
//! 检查在触发器上的状态（precheck、基线、last_checked_at）只在这里改：换检查（[`set_in`]）从新基线开始；成功
//! （[`passed_in`]：记检查时间、清掉检查的出错、changed 记基线）；失败（[`failed`]：记检查时间）。投信、记出错与告警由
//! [`crate::service`] 编排。

use muliao_platform::ids::AgentId;
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use muliao_assets::secrets::SecretUse;
use muliao_platform::db::Tx;
use muliao_workspace::commands::{AgentCommands, Options};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use similar::TextDiff;
use std::sync::Arc;

use crate::faults::{FaultSource, recovered};
use crate::store::{Trigger, set_baseline_in};
use muliao_platform::errors::{Error, Result};

/// 报错里带上的 stderr 结尾
const STDERR_TAIL_CHARS: usize = 2000;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum FireWhen {
    /// stdout 非空就触发
    #[default]
    Output,
    /// stdout 与上一次不同才触发（第一次只记录）
    Changed,
}

/// 命令最多这么多字符
const SCRIPT_MAX_CHARS: usize = 4000;

/// agent 写的一条 bash 命令（前置检查、流式命令）：1 到 4000 个字符、不全是空白。只经 [`Script::new`] 构造（反序列化
/// 也走它），拿到的就是合法的。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(into = "String", try_from = "String")]
#[schema(value_type = String)]
pub struct Script(String);

impl Script {
    pub fn new(text: impl Into<String>) -> std::result::Result<Self, String> {
        let text = text.into();
        let n = text.chars().count();
        if text.trim().is_empty() || n > SCRIPT_MAX_CHARS {
            return Err(format!("command must be 1 to {SCRIPT_MAX_CHARS} characters, got {n}"));
        }
        Ok(Self(text))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<String> for Script {
    type Error = String;

    fn try_from(text: String) -> std::result::Result<Self, String> {
        Self::new(text)
    }
}

impl From<Script> for String {
    fn from(s: Script) -> String {
        s.0
    }
}

/// 一个前置检查（存在触发器上，也是工具与接口的参数）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct Precheck {
    /// bash command run before each firing.
    // 上限同 SCRIPT_MAX_CHARS（属性里写不了常量）
    #[schemars(with = "String", length(min = 1, max = 4000))]
    pub command: Script,
    /// Vault keys exposed to the command as env vars.
    #[serde(default)]
    pub secrets: Vec<String>,
    /// 'output': fire when stdout is non-empty. 'changed': fire when stdout differs from the last run (the first run
    /// only records it).
    #[serde(default)]
    pub fire_when: FireWhen,
}

/// 给界面看的前置检查：每个字段都在（输入时能省的已取了默认值）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
pub struct PrecheckOut {
    pub command: Script,
    pub secrets: Vec<String>,
    pub fire_when: FireWhen,
}

impl From<&Precheck> for PrecheckOut {
    fn from(p: &Precheck) -> Self {
        // 解构：Precheck 加了字段而这里没跟上时编译不过
        let Precheck { command, secrets, fire_when } = p;
        PrecheckOut { command: command.clone(), secrets: secrets.clone(), fire_when: *fire_when }
    }
}

/// 检查本身失败的原因（给用户与 agent 看，可能含脚本的 stderr）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failed(pub String);

/// 检查的输出与上一次的输出决定的事：交给 agent 的事件内容（None：这次不触发）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decision {
    pub event: Option<String>,
    /// 第一次 changed 检查：只记录基线
    pub baseline: bool,
}

/// 由检查的输出与上一次的输出决定是否触发、交给 agent 什么（纯逻辑）。
pub fn decide(
    p: &Precheck,
    output: &str,
    previous: Option<&str>,
    max_bytes: usize,
) -> std::result::Result<Decision, Failed> {
    match p.fire_when {
        FireWhen::Output => {
            Ok(Decision { event: (!output.trim().is_empty()).then(|| output.to_owned()), baseline: false })
        }
        FireWhen::Changed => {
            let Some(previous) = previous else { return Ok(Decision { event: None, baseline: true }) };
            if output == previous {
                return Ok(Decision { event: None, baseline: false });
            }
            let event = format!("<diff>\n{}\n</diff>\n<output>\n{output}\n</output>", unified_diff(previous, output));
            if event.len() > max_bytes {
                return Err(Failed(format!("precheck diff and output exceed the {max_bytes}-byte event limit")));
            }
            Ok(Decision { event: Some(event), baseline: false })
        }
    }
}

/// 按行比较的 unified diff（同 Python 的 `difflib.unified_diff(splitlines, "previous", "current", lineterm="")`：
/// 行尾有没有换行不算差别）。
fn unified_diff(previous: &str, current: &str) -> String {
    let whole = |s: &str| s.lines().map(|l| format!("{l}\n")).collect::<String>();
    let (a, b) = (whole(previous), whole(current));
    let diff = TextDiff::from_lines(&a, &b);
    diff.unified_diff().context_radius(3).header("previous", "current").to_string().trim_end().to_owned()
}

/// 换上（或去掉）检查：新的检查从新的基线开始比较（旧命令的出错由调用方按人工干预清掉）。
pub fn set_in(tx: &mut Tx<'_>, t: &mut Trigger, precheck: Option<Precheck>) -> Result<()> {
    t.precheck = precheck;
    set_baseline_in(tx, t.id, None)
}

/// 检查正常结束、判定也成功且这次要认账：记下检查时间、清掉检查的出错，changed 检查存下这次的输出作基线。调用方写回这一行。
pub fn passed_in(tx: &mut Tx<'_>, t: &mut Trigger, p: &Precheck, output: &str, now: DateTime<Utc>) -> Result<()> {
    t.last_checked_at = Some(now);
    recovered(t, FaultSource::Precheck);
    if p.fire_when == FireWhen::Changed {
        set_baseline_in(tx, t.id, Some(output))?;
    }
    Ok(())
}

/// 检查失败：记下检查时间（原因由调用方记成出错）。
pub fn failed(t: &mut Trigger, now: DateTime<Utc>) {
    t.last_checked_at = Some(now);
}

/// 前置检查从正常变为出错时交给触发器所属 agent 的信：出错的是哪个检查、原因，以及怎么处理。
pub fn alert_letter(t: &Trigger, p: &Precheck, error: &str) -> String {
    let mut lines = vec![
        format!(
            "The precheck of your trigger `{}` (kind {}) just started failing, so the trigger did not fire this time. \
             You wrote this check, so it is yours to fix; the user has not been told.",
            t.name,
            t.mode.kind().name()
        ),
        String::new(),
        format!("fire_when: {}", serde_json::to_value(p.fire_when).expect("serializes").as_str().unwrap_or("")),
        format!("secrets: {}", if p.secrets.is_empty() { "(none)".to_owned() } else { p.secrets.join(", ") }),
        "command:".into(),
        "```bash".into(),
        p.command.as_str().to_owned(),
        "```".into(),
        "error:".into(),
        "```".into(),
        error.to_owned(),
        "```".into(),
        String::new(),
        "What to do:".into(),
    ];
    let user_only = "- Something only the user can fix (a missing secret, a permission, an account): `notify_user` \
                     with what is needed.";
    if t.finished_at.is_none() {
        lines.extend([
            "- A transient fault (network blip, a remote service briefly down): nothing to fix, the next check \
             recovers by itself; consider adding retries to the command if this keeps happening."
                .to_owned(),
            "- A bug in the command: fix it with `trigger_update` (new precheck) and verify with `trigger_run`.".into(),
            user_only.into(),
            "You are told only when the check goes from healthy to failing; while it keeps failing no further \
             message comes."
                .into(),
        ]);
    } else {
        // 已结束的触发器不会再触发，也不能再修改或运行：任务没做，要么自己做，要么重建
        lines.extend([
            "- This trigger has finished: this was its last firing, so its task was NOT done, and it can no longer \
             be updated or run. If the cause was transient or a bug in the command, do the task yourself or create \
             a new trigger with a fixed precheck."
                .to_owned(),
            user_only.into(),
            "Its prompt was:".into(),
            "<prompt>".into(),
            t.prompt.as_str().to_owned(),
            "</prompt>".into(),
        ]);
    }
    lines.join("\n")
}

/// 跑前置检查。
pub struct PrecheckRunner {
    commands: Arc<AgentCommands>,
    timeout: Duration,
    max_bytes: usize,
}

impl PrecheckRunner {
    pub fn new(commands: Arc<AgentCommands>, timeout: Duration, max_bytes: usize) -> Self {
        Self { commands, timeout, max_bytes }
    }

    pub fn max_bytes(&self) -> usize {
        self.max_bytes
    }

    /// 执行检查，返回它的输出，检查本身失败时 `Err(Failed)`；是否触发由 [`decide`] 决定。起不了命令（系统出错）是外层的错。
    pub async fn run(
        &self,
        agent_id: &AgentId,
        p: &Precheck,
        payload: Option<&str>,
    ) -> Result<std::result::Result<String, Failed>> {
        let shell = match self.commands.prepare(agent_id, &p.secrets, SecretUse::AgentCommand).await {
            Ok(shell) => shell,
            Err(Error::Domain(e)) => return Ok(Err(Failed(format!("precheck could not start: {e}")))),
            Err(e) => return Err(e),
        };
        let started = Instant::now();
        let options = Options {
            stdin: Some(payload.unwrap_or("").as_bytes().to_vec()),
            // 「curl … | jq」里 curl 失败、jq 对空输入照样退出 0：不开 pipefail 时检查的故障会变成静默的「没事」
            pipefail: true,
            merge_stderr: false,
        };
        let done = shell.run(p.command.as_str(), self.timeout, options).await?;
        let result = done.result;
        let Some(code) = result.exit_code else {
            return Ok(Err(Failed(format!("precheck timed out after {}s", self.timeout.as_secs_f64()))));
        };
        if code != 0 {
            let stderr = tail_chars(&result.stderr, STDERR_TAIL_CHARS);
            let refusal = done.refusal.map(|r| format!("\n{r}")).unwrap_or_default();
            return Ok(Err(Failed(format!("precheck exited with {code}: {}{refusal}", stderr.trim()))));
        }
        let size = result.stdout.len();
        if size > self.max_bytes {
            return Ok(Err(Failed(format!(
                "precheck output is {size} bytes, over the {}-byte event limit",
                self.max_bytes
            ))));
        }
        // 每分钟一次的检查绝大多数是空跑，记 info 会占满日志（#413）；触发与失败另有 info / warn
        tracing::debug!(agent = %agent_id, seconds = started.elapsed().as_secs_f64(), bytes = size, "precheck done");
        Ok(Ok(result.stdout))
    }
}

/// 最后 n 个字符。
pub(crate) fn tail_chars(text: &str, n: usize) -> &str {
    let count = text.chars().count();
    match text.char_indices().nth(count.saturating_sub(n)) {
        Some((i, _)) if count > n => &text[i..],
        _ => text,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(fire_when: FireWhen) -> Precheck {
        Precheck { command: Script::new("x").unwrap(), secrets: vec![], fire_when }
    }

    #[test]
    fn output_fires_on_non_blank_stdout() {
        let p = check(FireWhen::Output);
        assert_eq!(decide(&p, " \n", None, 100).unwrap().event, None);
        assert_eq!(decide(&p, "hi\n", None, 100).unwrap().event.as_deref(), Some("hi\n"));
    }

    #[test]
    fn changed_records_a_baseline_then_fires_with_a_diff() {
        let p = check(FireWhen::Changed);
        assert_eq!(decide(&p, "a\n", None, 1000).unwrap(), Decision { event: None, baseline: true });
        assert_eq!(decide(&p, "a\n", Some("a\n"), 1000).unwrap().event, None);
        let event = decide(&p, "a\nc\n", Some("a\nb"), 1000).unwrap().event.unwrap();
        assert_eq!(
            event,
            "<diff>\n--- previous\n+++ current\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n</diff>\n<output>\na\nc\n\n</output>"
        );
        assert!(decide(&p, "a\nc\n", Some("a\nb"), 20).is_err());
    }

    #[test]
    fn tails_by_chars() {
        assert_eq!(tail_chars("中文字符", 2), "字符");
        assert_eq!(tail_chars("ab", 5), "ab");
    }
}
