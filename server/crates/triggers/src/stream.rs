//! 流式触发：config = `{"command": "<bash 命令>", "secrets": [...], "debounce_seconds": 5}`。
//!
//! 平台托管一条常驻命令（如 Slack Socket Mode 监听），它的 stdout 每一行是一个事件：
//! - 命令在该 agent 的工作目录里用 `bash -o pipefail` 执行，环境与 run_shell、前置检查相同（最小环境变量、按 key 注入 secret、
//!   输出里的 secret 替换掉、自成进程组）。随服务启停；停用、删除、改配置时杀掉整个进程组，改了配置用新配置重起。
//! - 退出（不论退出码：它本该一直运行）或起不来都算一次失败，按退避重启（1、2、4……秒，最多 5 分钟）。偶尔退出重连是常态，
//!   连续快速失败达到 [`ALERT_AFTER`] 次才算脚本出错（[`crate::faults`]：原因含 stderr 日志的结尾，被写沙箱拦下的写补上说明，
//!   新出现时告警一次）；稳定运行 [`STABLE`] 算恢复：清除出错、退避从头算。进程的运行状态（running_since、failures）由这里
//!   维护，每个托管开始时清零：服务重启、换配置都从头算（出错不随服务重启清除：还坏着就不再告警一次）。创建、改配置、重新
//!   启用时 configure 换一个新的 generation：托管按它重起（停用再启用来不及被对账看到时也一定重起）。
//! - stderr 逐块脱敏后追加进工作目录下的 `trigger-logs/<名称>.stderr.log`（与后台任务共用 `muliao_workspace::output`；每次
//!   启动前超过 1 MiB 就轮换一份 .1），agent 可以自己读。
//! - 事件按 debounce 窗口合并：第一行到达后再等 debounce_seconds，期间到达的一起成为一次触发，事件内容是这些行。上一次触发
//!   开出的会话还没做完时事件排队、不重叠，它结束后合并投递；挡路的会话在等待（可能永远等不到）时通知用户一次（这份托管里
//!   每个挡路会话一次）。排队的事件以事件上限为界：排满了就不再读命令的输出，管道写满后命令自己阻塞——不丢、不重叠，内存与
//!   并行会话都有界。单行超过事件上限的丢弃并记日志。
//! - 排队的事件只在内存里：服务重启、改配置、停用时丢失；一批开始触发后不再重试（至多一次）；命令退出时管道里没读的输出只
//!   限时读。命令自己做断线重连、去重与游标，重要的来源另加轮询兜底（skill triggers）。

use std::collections::{BTreeMap, HashMap};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::{DateTime, Utc};
use muliao_notifications::{Notifications, Notify};
use muliao_platform::shell::{DRAIN, KillOnDrop, Output, Spawned, kill_group, redact};
use muliao_workspace::commands::{AgentCommands, Options};
use muliao_workspace::output::{record_output, tail};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::AsyncReadExt;
use tokio::sync::Notify as Wakeup;
use tokio::task::JoinHandle;
use tokio::time::Instant;

use crate::error::{Error, Result, invalid};
use crate::faults::{FaultSource, TriggerFault, recovered};
use crate::kinds::{Configured, TriggerKind, parse_config};
use crate::precheck::tail_chars;
use crate::service::{Busy, EVENT_MAX_BYTES, TriggerService, busy_previous_in};
use crate::store::{Listing, Trigger, load_in, save_in};

pub const KIND: &str = "stream";
/// 稳定运行这么久算恢复：清除出错，退避与连续失败计数从头算
pub const STABLE: Duration = Duration::from_secs(60);
/// 连续快速失败这么多次才算脚本出错
pub const ALERT_AFTER: u32 = 3;
const MAX_BACKOFF: Duration = Duration::from_secs(300);
/// 上一次触发的会话还没做完时，隔多久再看一次
const BUSY_POLL: Duration = Duration::from_secs(2);
const ERROR_TAIL_CHARS: usize = 2000;
const LOG_MAX_BYTES: u64 = 1024 * 1024;
const LOG_DIR: &str = "trigger-logs";
/// 对账时列不出触发器（数据库出错）隔多久再试
const RECONCILE_RETRY: Duration = Duration::from_secs(10);
const CHUNK: usize = 65536;

fn default_debounce() -> f64 {
    5.0
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamConfig {
    pub command: String,
    #[serde(default)]
    pub secrets: Vec<String>,
    #[serde(default = "default_debounce")]
    pub debounce_seconds: f64,
}

/// 托管进程的运行状态（存在 state 里）。
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
struct StreamState {
    /// configure 每次换一个：改配置、重新启用都重起
    generation: Option<String>,
    /// 当前进程的启动时间；没在运行时为 null
    running_since: Option<DateTime<Utc>>,
    #[serde(default)]
    failures: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct StreamStatus {
    /// 当前进程的启动时间；没有在运行（停用、刚启用或改配置后还没起来、退出后在等重启）时为 null
    pub running_since: Option<DateTime<Utc>>,
    /// 连续快速失败的次数（达到告警次数时原因在触发器的 faults 里）
    pub failures: u32,
    /// stderr 写到哪个文件（相对该 agent 的工作目录）
    pub stderr_log: String,
}

pub fn stderr_log(name: &str) -> String {
    format!("{LOG_DIR}/{name}.stderr.log")
}

/// 流式这种方式的纯逻辑部分。
pub struct Stream;

impl TriggerKind for Stream {
    fn name(&self) -> &'static str {
        KIND
    }

    fn description(&self) -> &str {
        "Runs a long-lived command hosted by the platform; every line it prints to stdout is an event. config: \
         {\"command\": \"<bash command>\", \"secrets\": [\"VAULT_KEY\", ...], \"debounce_seconds\": 5}. The command \
         runs in the agent's workspace with the secrets as env vars; it is restarted with backoff whenever it exits, \
         and killed when the trigger is disabled, deleted or reconfigured. To restart it (e.g. after editing the \
         script it runs), disable and re-enable it; status.running_since is null until the new process has started. \
         Lines arriving within debounce_seconds of the first one fire together (one conversation, the lines are the \
         event); while the conversation the previous firing started is still busy, events queue and are delivered \
         together once it is done. stderr goes to trigger-logs/<name>.stderr.log in the workspace. Queued events are \
         lost on a restart, so the command handles reconnects, dedup and cursors itself; read skill triggers before \
         writing one."
    }

    fn configure(&self, config: &Value, _now: DateTime<Utc>) -> Result<Configured> {
        let c: StreamConfig = parse_config(KIND, config)?;
        let n = c.command.chars().count();
        if c.command.trim().is_empty() || n > 4000 {
            return Err(invalid(format!("stream command must be 1 to 4000 characters, got {n}")));
        }
        if !(0.0..=3600.0).contains(&c.debounce_seconds) {
            return Err(invalid(format!("debounce_seconds must be between 0 and 3600, got {}", c.debounce_seconds)));
        }
        // 创建、改配置、重新启用都要重起进程：换一个 generation，运行状态在同一事务里清零，新进程登记之前不显示旧进程的
        let state = StreamState { generation: Some(uuid::Uuid::new_v4().simple().to_string()), ..Default::default() };
        Ok(Configured {
            config: serde_json::to_value(c).expect("serializes"),
            state: serde_json::to_value(state).expect("serializes"),
        })
    }

    fn secrets(&self, config: &Value) -> Vec<String> {
        serde_json::from_value::<StreamConfig>(config.clone()).map(|c| c.secrets).unwrap_or_default()
    }

    fn status(&self, t: &Trigger) -> Result<Value> {
        let state: StreamState = t.state_as()?;
        let status = StreamStatus {
            // 停用时进程已被杀掉（关停时来不及写回，所以按启用状态推导，而不是信存下的值）
            running_since: if t.enabled { state.running_since } else { None },
            failures: state.failures,
            stderr_log: stderr_log(&t.name),
        };
        Ok(serde_json::to_value(status).expect("serializes"))
    }
}

/// 流式命令连续快速退出、刚算作出错时交给触发器所属 agent 的信。
fn stream_alert(t: &Trigger, fault: &str) -> String {
    let config: Option<StreamConfig> = serde_json::from_value(t.config.clone()).ok();
    let (secrets, command) = match &config {
        Some(c) if !c.secrets.is_empty() => (c.secrets.join(", "), c.command.as_str()),
        Some(c) => ("(none)".to_owned(), c.command.as_str()),
        None => ("(unreadable config)".to_owned(), ""),
    };
    [
        format!(
            "The stream command of your trigger `{}` keeps exiting; the platform keeps restarting it with backoff, but \
             no events come in while it is down. You wrote this command, so it is yours to fix; the user has not been \
             told.",
            t.name
        ),
        String::new(),
        format!("secrets: {secrets}"),
        "command:".into(),
        "```bash".into(),
        command.to_owned(),
        "```".into(),
        "error:".into(),
        "```".into(),
        fault.to_owned(),
        "```".into(),
        format!("Full stderr: {} in your workspace.", stderr_log(&t.name)),
        String::new(),
        "What to do:".into(),
        format!(
            "- A transient fault (network down, a remote service briefly unavailable): nothing to fix; it counts as \
             recovered once it runs for {} seconds.",
            STABLE.as_secs()
        ),
        "- A bug in the command: fix it with `trigger_update` (a new config restarts it); after editing a script it \
         runs, restart it by disabling and re-enabling the trigger."
            .into(),
        "- Something only the user can fix (a missing secret, a permission, an account): `notify_user` with what is \
         needed."
            .into(),
        "You are told only once until it runs stably again or you change the trigger; `trigger_list` shows the error \
         in `faults`."
            .into(),
    ]
    .join("\n")
}

/// 一个托管进程由什么决定：其中任何一项变了，就杀掉旧的按新的重起。
#[derive(Debug, Clone, PartialEq)]
struct Spec {
    trigger_id: i64,
    name: String,
    agent_id: String,
    config: Value,
    generation: Option<String>,
}

impl Spec {
    fn of(t: &Trigger) -> Self {
        Spec {
            trigger_id: t.id,
            name: t.name.clone(),
            agent_id: t.agent_id.clone(),
            config: t.config.clone(),
            generation: t.state.get("generation").and_then(Value::as_str).map(str::to_owned),
        }
    }
}

/// 排队等投递的事件（只在内存里）：命令输出的行，以事件上限为界——已有内容、再加一行会超过上限时读取方等着，命令的输出
/// 留在管道里（第一行总是收下）。
struct Pending {
    max_bytes: usize,
    batch: Mutex<Batch>,
    changed: Wakeup,
}

#[derive(Default)]
struct Batch {
    lines: Vec<String>,
    /// 合成文本的字节数
    size: usize,
    /// 这一批第一行到达的时间，debounce 从它算起
    first_at: Option<Instant>,
}

impl Pending {
    fn new(max_bytes: usize) -> Self {
        Self { max_bytes, batch: Mutex::default(), changed: Wakeup::new() }
    }

    fn len(&self) -> usize {
        self.batch.lock().expect("pending lock").lines.len()
    }

    async fn put(&self, line: String) {
        let n = line.len() + 1;
        loop {
            let notified = self.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            {
                let mut b = self.batch.lock().expect("pending lock");
                if b.lines.is_empty() || b.size + n <= self.max_bytes {
                    if b.lines.is_empty() {
                        b.first_at = Some(Instant::now());
                    }
                    b.lines.push(line);
                    b.size += n;
                    self.changed.notify_waiters();
                    return;
                }
            }
            notified.await;
        }
    }

    /// 等到有内容、且第一行到达后过了 debounce。
    async fn ready(&self, debounce: Duration) {
        let first = loop {
            let notified = self.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if let Some(first) = self.batch.lock().expect("pending lock").first_at {
                break first;
            }
            notified.await;
        };
        tokio::time::sleep_until(first + debounce).await;
    }

    /// 取出这一批：(事件文本, 行数)。
    fn take(&self) -> (String, usize) {
        let b = std::mem::take(&mut *self.batch.lock().expect("pending lock"));
        self.changed.notify_waiters();
        (b.lines.join("\n"), b.lines.len())
    }
}

/// 写回托管进程的运行状态。
#[derive(Debug, Default)]
struct Record {
    running_since: Option<Option<DateTime<Utc>>>,
    failures: Option<u32>,
    /// 稳定运行了一段时间：清掉出错
    stable: bool,
    /// 脚本出错（记下，新出现时告警）
    fault: Option<String>,
}

/// 节奏（测试改短）。
#[derive(Clone, Copy)]
pub struct Timing {
    pub stable: Duration,
    pub busy_poll: Duration,
    pub backoff: fn(u32) -> Duration,
}

fn backoff(failures: u32) -> Duration {
    Duration::from_secs(1u64 << failures.saturating_sub(1).min(16)).min(MAX_BACKOFF)
}

impl Default for Timing {
    fn default() -> Self {
        Self { stable: STABLE, busy_poll: BUSY_POLL, backoff }
    }
}

/// 流式触发的激活机制：为每个启用的流式触发器托管一个进程，把它的输出行合并成触发。
pub struct StreamRunner {
    service: TriggerService,
    commands: Arc<AgentCommands>,
    notifications: Notifications,
    timing: Timing,
    running: Mutex<HashMap<i64, (Spec, JoinHandle<()>)>>,
}

impl StreamRunner {
    pub fn new(
        service: TriggerService,
        commands: Arc<AgentCommands>,
        notifications: Notifications,
        timing: Timing,
    ) -> Self {
        Self { service, commands, notifications, timing, running: Mutex::default() }
    }

    /// 常驻：按触发器的变化对账——该运行的起起来，停用、删除或改了配置的取消（取消即杀掉进程组）。每个托管是一个独立的
    /// tokio 任务：一个触发器出意外不影响别的。
    pub fn run(self: Arc<Self>) -> JoinHandle<()> {
        tokio::spawn(async move {
            let mut changed = self.service.watch();
            loop {
                changed.borrow_and_update();
                // 列不出来（数据库出错）时不干等下一次改动：过一会儿自己重试
                let reconciled = self.reconcile().await;
                tokio::select! {
                    r = changed.changed() => if r.is_err() { return },
                    _ = tokio::time::sleep(RECONCILE_RETRY), if !reconciled => {}
                }
            }
        })
    }

    /// 对账一次；列不出触发器时返回 false。
    async fn reconcile(self: &Arc<Self>) -> bool {
        let listing = Listing { kind: Some(KIND.into()), enabled_only: true, ..Listing::default() };
        let wanted: HashMap<i64, Spec> = match self.service.list(listing).await {
            Ok(ts) => ts.iter().map(|t| (t.id, Spec::of(t))).collect(),
            Err(e) => {
                tracing::error!(error = %e, "listing stream triggers failed; retrying");
                return false;
            }
        };
        let mut running = self.running.lock().expect("running lock");
        running.retain(|id, (spec, handle)| {
            let keep = wanted.get(id) == Some(spec);
            if !keep {
                handle.abort();
                tracing::info!(trigger = %spec.name, "stream stopped");
            }
            keep
        });
        for (id, spec) in wanted {
            if !running.contains_key(&id) {
                let this = self.clone();
                let handle = tokio::spawn({
                    let spec = spec.clone();
                    async move { this.supervise(spec).await }
                });
                running.insert(id, (spec, handle));
            }
        }
        true
    }

    /// 关停：杀掉全部托管的命令（等它们的任务收尾）。
    pub async fn shutdown(&self) {
        let handles: Vec<JoinHandle<()>> =
            self.running.lock().expect("running lock").drain().map(|(_, (_, h))| h).collect();
        for h in &handles {
            h.abort();
        }
        for h in handles {
            let _ = h.await;
        }
    }

    async fn supervise(&self, spec: Spec) {
        let config: StreamConfig = match serde_json::from_value(spec.config.clone()) {
            Ok(c) => c,
            Err(e) => {
                // 配置只经 configure 写入；存下的坏了是数据问题，只停用这一个
                let reason = format!("invalid stored stream config: {e}");
                if let Err(e) = self.service.disable(spec.trigger_id, reason).await {
                    tracing::error!(trigger = %spec.name, error = %e, "disabling a broken stream trigger failed");
                }
                return;
            }
        };
        let pending = Pending::new(EVENT_MAX_BYTES);
        tokio::join!(self.deliver_loop(&spec, &config, &pending), self.process_loop(&spec, &config, &pending));
    }

    // ---- 进程 ----

    async fn process_loop(&self, spec: &Spec, config: &StreamConfig, pending: &Pending) {
        // 这份托管从头算：服务重启前的进程留下的运行状态清掉（创建、改配置、重新启用时 configure 已清过）
        self.record(spec, Record { running_since: Some(None), failures: Some(0), ..Record::default() }).await;
        let mut failures = 0;
        loop {
            let (error, stable) = self.run_once(spec, config, pending).await;
            // 稳定运行过的不算连续失败，从最短的退避重来
            if stable {
                failures = 0;
            }
            failures += 1;
            tracing::warn!(trigger = %spec.name, failures, error, "stream exited");
            let fault = (failures >= ALERT_AFTER).then(|| {
                format!("exited quickly {failures} times in a row, restarting with backoff; last exit: {error}")
            });
            self.record(spec, Record { running_since: Some(None), failures: Some(failures), fault, stable: false })
                .await;
            tokio::time::sleep((self.timing.backoff)(failures)).await;
        }
    }

    /// 起一次进程并跑到它退出，返回 (退出的原因, 是否稳定运行过)。
    async fn run_once(&self, spec: &Spec, config: &StreamConfig, pending: &Pending) -> (String, bool) {
        let shell = match self.commands.prepare(&spec.agent_id, &config.secrets, true).await {
            Ok(shell) => shell,
            Err(e) => return (format!("stream secrets unavailable: {e}"), false),
        };
        let log_file = shell.cwd.join(stderr_log(&spec.name));
        let started = format!("--- started {} ---", Utc::now().to_rfc3339());
        if let Err(e) = prepare_log(log_file.clone(), format!("{started}\n")).await {
            return (format!("stream supervision failed: cannot write {}: {e}", log_file.display()), false);
        }
        let options = Options { pipefail: true, ..Options::default() };
        let Spawned { mut child, pgid, stdout, stderr, .. } = match shell.spawn(&config.command, false, &options) {
            Ok(s) => s,
            Err(e) => return (format!("stream supervision failed: cannot start the command: {e}"), false),
        };
        // spawn 之后的任何一步（包括被取消：停用、删除、改配置、关停）都要杀掉进程组
        let mut guard = KillOnDrop(Some(pgid));
        tracing::info!(trigger = %spec.name, pid = pgid, "stream started");
        self.record(spec, Record { running_since: Some(Some(Utc::now())), ..Record::default() }).await;
        let mut stderr = stderr.expect("stderr is not merged");
        let reading = self.read_events(spec, stdout, &shell.secrets, pending);
        let logging = record_output(&mut stderr, &log_file, &shell.secrets);
        let stable_timer = tokio::time::sleep(self.timing.stable);
        tokio::pin!(reading, logging, stable_timer);
        let (mut read_done, mut log_done, mut stable) = (false, false, false);
        let mut failure = None;
        let status = loop {
            tokio::select! {
                s = child.wait() => break s.ok(),
                _ = &mut stable_timer, if !stable => {
                    stable = true;
                    self.record(spec, Record { stable: true, failures: Some(0), ..Record::default() }).await;
                }
                _ = &mut reading, if !read_done => read_done = true,
                r = &mut logging, if !log_done => {
                    log_done = true;
                    if let Err(e) = r {
                        // 记不下 stderr：不让它没人管地跑着
                        failure = Some(format!("stream supervision failed: recording stderr: {e}"));
                        kill_group(pgid);
                    }
                }
            }
        };
        // 命令退出了，它留下的后代不能跟着下一次启动越积越多：杀掉整组（组里还有成员时这个进程组 id 不会被复用），剩下的
        // 输出限时读完
        kill_group(pgid);
        guard.0 = None;
        let drain = async {
            tokio::join!(
                async {
                    if !read_done {
                        (&mut reading).await;
                    }
                },
                async {
                    if !log_done {
                        let _ = (&mut logging).await;
                    }
                }
            )
        };
        let _ = tokio::time::timeout(DRAIN, drain).await;
        let code = status.map(|s| s.code().unwrap_or_else(|| -s.signal().unwrap_or(0)));
        let path = log_file.clone();
        let logged = tokio::task::spawn_blocking(move || tail(&path).unwrap_or_default()).await.unwrap_or_default();
        // 日志是追加的，末尾几行里可能有前几次启动的报错：补说明只看这次的分隔行之后（分隔行已不在末尾时整段都是这次的）
        let this_run = logged.rsplit_once(&started).map_or(logged.as_str(), |(_, after)| after);
        let refusal = shell.refusal(code, this_run).map(|r| format!("\n{r}")).unwrap_or_default();
        let error = failure.unwrap_or_else(|| {
            let code = code.map_or("no exit code".to_owned(), |c| c.to_string());
            format!("stream command exited with {code}: {}", tail_chars(&logged, ERROR_TAIL_CHARS).trim())
        });
        (format!("{error}{refusal}"), stable)
    }

    async fn read_events(
        &self,
        spec: &Spec,
        mut stdout: Output,
        secrets: &BTreeMap<String, String>,
        pending: &Pending,
    ) {
        let mut buf: Vec<u8> = Vec::new();
        // 正在丢弃一个超长行的剩余部分（直到下一个换行）
        let mut skipping = false;
        let mut chunk = vec![0u8; CHUNK];
        loop {
            let n = match stdout.read(&mut chunk).await {
                Ok(0) | Err(_) => break,
                Ok(n) => n,
            };
            buf.extend_from_slice(&chunk[..n]);
            while let Some(end) = buf.iter().position(|b| *b == b'\n') {
                let mut raw: Vec<u8> = buf.drain(..=end).collect();
                raw.pop();
                if std::mem::take(&mut skipping) {
                    continue;
                }
                self.emit(spec, &raw, secrets, pending).await;
            }
            if buf.len() > pending.max_bytes {
                tracing::warn!(trigger = %spec.name, "stream line over the event limit dropped");
                buf.clear();
                skipping = true;
            }
        }
        if !buf.is_empty() && !skipping {
            self.emit(spec, &buf, secrets, pending).await;
        }
    }

    async fn emit(&self, spec: &Spec, raw: &[u8], secrets: &BTreeMap<String, String>, pending: &Pending) {
        let line = redact(String::from_utf8_lossy(raw).trim_end_matches('\r'), secrets);
        if line.trim().is_empty() {
            return;
        }
        if line.len() + 1 > pending.max_bytes {
            tracing::warn!(trigger = %spec.name, "stream line over the event limit dropped");
            return;
        }
        pending.put(line).await;
    }

    // ---- 投递 ----

    async fn deliver_loop(&self, spec: &Spec, config: &StreamConfig, pending: &Pending) {
        // 已为哪个挡路的会话通知过用户（这份托管里每个挡路会话只通知一次）
        let mut notified: Option<String> = None;
        let debounce = Duration::from_secs_f64(config.debounce_seconds);
        loop {
            pending.ready(debounce).await;
            if let Err(e) = self.wait_previous(spec, pending, &mut notified).await {
                // 契约之外的失败（如数据库出错）：事件留在队列里，稍后再试
                tracing::error!(trigger = %spec.name, error = %e, "stream delivery could not check the previous session");
                tokio::time::sleep(self.timing.busy_poll).await;
                continue;
            }
            let (event, lines) = pending.take();
            if let Err(e) = self.service.fire_activated(spec.trigger_id, Some(event)).await {
                // 一旦开始触发就不重试这一批（至多一次）：可能已经提交了投信才出错，重试会重复触发
                tracing::error!(trigger = %spec.name, lines, error = %e, "stream delivery failed, batch dropped");
                tokio::time::sleep(self.timing.busy_poll).await;
            }
        }
    }

    /// 上一次触发开出的会话还没做完时等着（事件继续排队，排满后命令阻塞）；挡路的会话在等待时通知用户一次。
    async fn wait_previous(&self, spec: &Spec, pending: &Pending, notified: &mut Option<String>) -> Result<()> {
        loop {
            let (trigger_id, queued, already) = (spec.trigger_id, pending.len(), notified.clone());
            let notifications = self.notifications.clone();
            // 先只读；只有要通知时才开写事务（忙的时候每轮都查，不占写线程）
            let blocking = self
                .service
                .db()
                .read(move |c| {
                    let Some(t) = load_in(c, trigger_id)? else { return Ok::<_, Error>(None) };
                    Ok(busy_previous_in(c, &t)?.map(|(session_id, busy)| (t, session_id, busy)))
                })
                .await?;
            let Some((t, session_id, busy)) = blocking else { return Ok(()) };
            if busy == Busy::Waiting && already.as_deref() != Some(session_id.as_str()) {
                let sid = session_id.clone();
                self.service
                    .db()
                    .write(move |tx| notifications.notify_in(tx, &waiting_notice(&t, queued, &sid)))
                    .await?;
                *notified = Some(session_id);
            }
            tokio::time::sleep(self.timing.busy_poll).await;
        }
    }

    // ---- 状态 ----

    /// 把进程的运行状态写回触发器。只写给还是这份配置、仍启用的触发器：被取消的旧进程不覆盖新进程的状态。写不进去（数据库
    /// 出错）不影响进程的监督：记日志。
    async fn record(&self, spec: &Spec, record: Record) {
        let (expected, faults) = (spec.clone(), self.service.faults().clone());
        let written = self
            .service
            .db()
            .write(move |tx| {
                let Some(mut t) = load_in(tx, expected.trigger_id)? else { return Ok::<_, Error>(()) };
                if !t.enabled || Spec::of(&t) != expected {
                    return Ok(());
                }
                let mut state: StreamState = t.state_as()?;
                if let Some(since) = record.running_since {
                    state.running_since = since;
                }
                if let Some(failures) = record.failures {
                    state.failures = failures;
                }
                t.set_state(&state);
                if record.stable {
                    recovered(&mut t, FaultSource::Stream);
                }
                if let Some(error) = record.fault {
                    let fault = TriggerFault { source: FaultSource::Stream, error: error.clone() };
                    faults.report_in(tx, &mut t, fault, |t| stream_alert(t, &error))?;
                }
                save_in(tx, &t)
            })
            .await;
        if let Err(e) = written {
            tracing::error!(trigger = %spec.name, error = %e, "stream state could not be recorded");
        }
    }
}

fn waiting_notice(t: &Trigger, queued: usize, session_id: &str) -> Notify {
    Notify {
        title: format!("流式触发器 {} 的事件在排队", t.name),
        body: format!(
            "上一次触发的会话在等待（后台任务、委派或你的回答），它结束前新事件都排着，现在有 {queued} 条。点开看它在等什么。"
        ),
        session_id: Some(session_id.to_owned()),
        issue_id: None,
    }
}

/// 每次启动前：建目录，超过上限就轮换一份 .1，再写一行分隔（agent 读日志时分得清是哪一次）。
async fn prepare_log(path: PathBuf, header: String) -> std::io::Result<()> {
    tokio::task::spawn_blocking(move || {
        use std::io::Write;
        std::fs::create_dir_all(path.parent().unwrap_or(Path::new(".")))?;
        if std::fs::metadata(&path).is_ok_and(|m| m.len() > LOG_MAX_BYTES) {
            let mut rotated = path.clone().into_os_string();
            rotated.push(".1");
            std::fs::rename(&path, rotated)?;
        }
        std::fs::OpenOptions::new().create(true).append(true).open(&path)?.write_all(header.as_bytes())
    })
    .await
    .expect("preparing the stream log panicked")
}
