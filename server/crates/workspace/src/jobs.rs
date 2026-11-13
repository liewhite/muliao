//! 后台任务：agent 在后台跑的 shell 命令，会话之外进行、结局交回会话的工作（会话领域的占用）的一种。做法同 Claude Code 的
//! 后台 bash：不占住运行、不设时限，结束时往发起它的会话的信箱投事件信件，运行期间按发起时给的间隔（heartbeat_seconds）发
//! 检查事件，由 agent 决定继续等还是结束它。
//!
//! - 输出（stdout 与 stderr 合并）逐块脱敏后写进 agent 工作目录的 jobs/<id>.log，agent 用 read_file 读完整输出；事件里只带
//!   最后几行。
//! - 进程以 agent 的身份起（[`AgentCommands`]，自成进程组），job_kill、会话被停止与关停杀掉整棵进程树。
//! - 启动（起进程、写任务行并登记占用、交给照看者）在一个独立的 tokio 任务里一气做完：发起它的运行被停止也不会停在半路。
//! - 一个任务从起来到结局写进库，一直由它的照看者（[`JobRunner::supervise`]）管：结局只由照看者写，与交回会话（投事件信件、
//!   释放占用）在同一个事务里（会话被停止时除外：结局照记，不投信）。job_kill、会话停止、关停只杀进程并说明结局算什么。
//! - 以进程退出为准（不是输出管道关闭）：退出后在有限时间内读完剩余输出，后台孙进程一直占着管道也不会让任务结束不了。
//! - 照看者没能写下结局（数据库出错，几乎不发生）时记 error 日志，这一行停在 running，下次服务启动时按「服务意外退出」
//!   记为被中断（[`JobRunner::recover`]：进程自成进程组、可能还活着，如实告知，不盲杀——pid 可能已被复用）。
//! - 任务对 agent 可见（工作目录本来就是 agent 级的，job_status / job_kill 按 agent 检查归属），事件归发起它的会话。日志
//!   大小不设上限。

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::{DateTime, Utc};
use muliao_platform::db::{Database, Tx};
use muliao_platform::shell::{DRAIN, LEFT_RUNNING_NOTE, Spawned, kill_group};
use muliao_sessions::events::SessionStopped;
use muliao_sessions::holds::HoldOf;
use muliao_sessions::work::{CheckBeats, SessionWork};
use rusqlite::{Connection, Row, params};
use tokio::sync::watch;

use crate::commands::{AgentCommands, AgentShell, Options};
use crate::output::{record_output, tail};
use muliao_platform::errors::{Error, Result, conflict, not_found};

/// 日志的最后几行；读不了（agent 把它换成了目录、改了权限）时换成一句说明，不让结局与检查因此写不下去。
fn log_tail(path: &std::path::Path) -> String {
    tail(path).unwrap_or_else(|e| format!("(could not read the log: {e})"))
}

pub const JOBS_DIR: &str = "jobs";
/// 运行中检查的节拍（同会话领域的委派检查）
pub const TICK: Duration = Duration::from_secs(5);
/// 关停时等照看者写完结局最多这么久
const SHUTDOWN_WAIT: Duration = Duration::from_secs(2 * DRAIN.as_secs());

/// 后台任务的状态（界面与 job_status 看到的）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Running,
    Exited,
    Killed,
    Interrupted,
}

impl JobStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            JobStatus::Running => "running",
            JobStatus::Exited => "exited",
            JobStatus::Killed => "killed",
            JobStatus::Interrupted => "interrupted",
        }
    }
}

/// 后台任务怎样结束的（结束的时刻在记下时另给，[`End::at`]）。code：退出码，被信号结束的是负的信号值，取不到为 None。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum End {
    /// 自己退出
    Exited { code: Option<i32> },
    /// 被 job_kill 或会话停止结束
    Killed { code: Option<i32> },
    /// 因服务停止被中断
    Interrupted,
}

impl End {
    fn at(self, at: DateTime<Utc>) -> JobState {
        JobState::Ended { end: self, at }
    }
}

/// 后台任务的状态：结束了的带结局与结束的时刻。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    Running,
    Ended { end: End, at: DateTime<Utc> },
}

impl JobState {
    pub fn status(self) -> JobStatus {
        match self {
            JobState::Running => JobStatus::Running,
            JobState::Ended { end: End::Exited { .. }, .. } => JobStatus::Exited,
            JobState::Ended { end: End::Killed { .. }, .. } => JobStatus::Killed,
            JobState::Ended { end: End::Interrupted, .. } => JobStatus::Interrupted,
        }
    }

    fn running(self) -> bool {
        self == JobState::Running
    }

    /// 存下的三列（status、exit_code、finished_at）的唯一解析。
    fn parse(status: &str, code: Option<i32>, at: Option<DateTime<Utc>>) -> Option<Self> {
        Some(match (status, code, at) {
            ("running", None, None) => JobState::Running,
            ("exited", code, Some(at)) => End::Exited { code }.at(at),
            ("killed", code, Some(at)) => End::Killed { code }.at(at),
            ("interrupted", None, Some(at)) => End::Interrupted.at(at),
            _ => return None,
        })
    }

    /// 存成三列。
    fn columns(self) -> (&'static str, Option<i32>, Option<DateTime<Utc>>) {
        match self {
            JobState::Running => ("running", None, None),
            JobState::Ended { end, at } => {
                let code = match end {
                    End::Exited { code } | End::Killed { code } => code,
                    End::Interrupted => None,
                };
                (self.status().as_str(), code, Some(at))
            }
        }
    }
}

/// 库里的一行。
#[derive(Debug, Clone)]
struct Job {
    id: i64,
    session_id: String,
    agent_id: String,
    command: String,
    state: JobState,
    log_path: String,
    pid: Option<i32>,
    check_seconds: f64,
    started_at: DateTime<Utc>,
}

const COLUMNS: &str =
    "id, session_id, agent_id, command, status, exit_code, log_path, pid, check_seconds, started_at, finished_at";

fn job_of(r: &Row<'_>) -> rusqlite::Result<Job> {
    let (status, code, at): (String, Option<i32>, Option<DateTime<Utc>>) = (r.get(4)?, r.get(5)?, r.get(10)?);
    let state = JobState::parse(&status, code, at).ok_or_else(|| {
        rusqlite::Error::FromSqlConversionFailure(
            4,
            rusqlite::types::Type::Text,
            format!("job status {status:?} with exit code {code:?} and finished_at {at:?}").into(),
        )
    })?;
    Ok(Job {
        id: r.get(0)?,
        session_id: r.get(1)?,
        agent_id: r.get(2)?,
        command: r.get(3)?,
        state,
        log_path: r.get(6)?,
        pid: r.get(7)?,
        check_seconds: r.get(8)?,
        started_at: r.get(9)?,
    })
}

fn jobs_where(c: &Connection, filter: &str, args: impl rusqlite::Params) -> Result<Vec<Job>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM jobs WHERE {filter} ORDER BY id"))?;
    Ok(stmt.query_map(args, job_of)?.collect::<rusqlite::Result<Vec<_>>>()?)
}

fn job_in(c: &Connection, job_id: i64) -> Result<Option<Job>> {
    Ok(jobs_where(c, "id = ?1", [job_id])?.pop())
}

/// 这个 agent 的任务（别的 agent 的按不存在报）。
fn own_in(c: &Connection, job_id: i64, agent_id: &str) -> Result<Job> {
    job_in(c, job_id)?.filter(|j| j.agent_id == agent_id).ok_or_else(|| not_found(format!("job {job_id} not found")))
}

/// 起来了的后台任务。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobStarted {
    pub id: i64,
    pub log_path: String,
}

/// job_status 与界面看到的一个任务。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobInfo {
    pub id: i64,
    pub command: String,
    pub state: JobState,
    pub log_path: String,
    pub running_for: String,
    pub tail: String,
}

/// 照看者之外的人对进程做了什么：结局按它记（没人动过就按进程自己退出记）。
#[derive(Debug, Clone, PartialEq, Eq)]
enum Ending {
    /// job_kill 或会话被停止
    Killed,
    /// 服务关停：原因
    Interrupted(String),
}

/// 进程现在怎样：照看者与 job_kill、会话停止、关停都在同一把锁里看和改，结局与 job_kill 的回答对得上。
enum Process {
    Running,
    /// 有人说明了结局并杀了进程组，还没回收
    Ending(Ending),
    /// 已被回收：进程组 id 可能已被复用，不再杀它
    Reaped,
}

/// 一个有人照看的任务（进程在跑，或已退出、结局还没写完）。
struct Handle {
    pgid: i32,
    process: Mutex<Process>,
    /// 照看者结束了（结局写完或写不进去）
    done: watch::Receiver<bool>,
}

impl Handle {
    /// 说明结局并杀掉进程组；进程已回收（结局正在记）时什么也不做，返回 false。先说明的算（job_kill 之后再关停仍记为被
    /// 结束）。
    fn end(&self, ending: Ending) -> bool {
        let mut process = self.process.lock().expect("process lock");
        match &*process {
            Process::Reaped => return false,
            Process::Running => *process = Process::Ending(ending),
            Process::Ending(_) => {}
        }
        kill_group(self.pgid);
        true
    }

    /// 照看者回收了进程：此后不再杀它；返回别人说明的结局。
    fn reaped(&self) -> Option<Ending> {
        match std::mem::replace(&mut *self.process.lock().expect("process lock"), Process::Reaped) {
            Process::Ending(e) => Some(e),
            _ => None,
        }
    }
}

fn duration(start: DateTime<Utc>, end: DateTime<Utc>) -> String {
    let seconds = (end - start).num_seconds().max(0);
    let (hours, minutes, seconds) = (seconds / 3600, seconds / 60 % 60, seconds % 60);
    if hours > 0 { format!("{hours}h{minutes:02}m{seconds:02}s") } else { format!("{minutes}m{seconds:02}s") }
}

/// 数字按 Python 的 `{:g}` 写（6 位有效数字，去掉末尾的 0）：工具文字与 Python 版一致。
pub fn format_g(x: f64) -> String {
    if x == 0.0 || !x.is_finite() {
        return if x == 0.0 { "0".to_owned() } else { x.to_string() };
    }
    let sci = format!("{x:.5e}");
    let (mantissa, exp) = sci.split_once('e').expect("scientific notation");
    let exp: i32 = exp.parse().expect("exponent");
    let strip = |s: String| if s.contains('.') { s.trim_end_matches('0').trim_end_matches('.').to_owned() } else { s };
    if (-4..6).contains(&exp) {
        strip(format!("{:.*}", (5 - exp) as usize, x))
    } else {
        let sign = if exp < 0 { '-' } else { '+' };
        format!("{}e{sign}{:02}", strip(mantissa.to_owned()), exp.abs())
    }
}

/// note：被中断时是原因；退出、被结束时是补充说明（如后代还占着输出），没有为 None。
fn final_text(job: &Job, end: End, now: DateTime<Utc>, last: &str, note: Option<&str>) -> String {
    let took = duration(job.started_at, now);
    let (head, note) = match end {
        End::Exited { code } => (
            format!(
                "Background job {} finished with exit code {} after {took}.",
                job.id,
                code.map_or("None".to_owned(), |c| c.to_string())
            ),
            note,
        ),
        End::Killed { .. } => (format!("Background job {} was killed (job_kill) after {took}.", job.id), note),
        End::Interrupted => (
            format!(
                "Background job {} was interrupted after {took}: {}. It may have partly run.",
                job.id,
                note.unwrap_or("")
            ),
            None,
        ),
    };
    let extra = note.map(|n| format!("\nNote: {n}")).unwrap_or_default();
    format!("{head}{extra}\nCommand: {}\n{}", job.command, output(job, last))
}

fn check_text(job: &Job, now: DateTime<Utc>, last: &str) -> String {
    format!(
        "Background job {id} is still running ({} so far).\nCommand: {}\n{}\nIf it looks stuck or is no longer needed, \
         call job_kill({id}); otherwise keep waiting (if nothing needs doing, reply with just '…'): you will get \
         another check-in in {} seconds and an event when it finishes.",
        duration(job.started_at, now),
        job.command,
        output(job, last),
        format_g(job.check_seconds),
        id = job.id,
    )
}

fn output(job: &Job, last: &str) -> String {
    let shown = if last.is_empty() { "No output yet.".to_owned() } else { format!("Last lines of output:\n{last}") };
    format!("{shown}\nFull output: {} (in your workspace)", job.log_path)
}

/// 后台任务的启动、照看、查看、结束与运行中检查。
pub struct JobRunner {
    db: Arc<Database>,
    commands: Arc<AgentCommands>,
    work: SessionWork,
    running: Mutex<HashMap<i64, Arc<Handle>>>,
    beats: Mutex<CheckBeats<i64>>,
}

impl JobRunner {
    pub fn new(db: Arc<Database>, commands: Arc<AgentCommands>, work: SessionWork) -> Arc<Self> {
        Arc::new(Self { db, commands, work, running: Mutex::default(), beats: Mutex::default() })
    }

    fn log_file(&self, job: &Job) -> PathBuf {
        // 与起进程时的 cwd 同源（agent 的工作目录）
        self.commands.workspace(&job.agent_id).join(&job.log_path)
    }

    /// 起一个后台任务。awaited：发起它的那次运行里用户是否在等回答（完成通知随它推送）。check_seconds：运行中检查的间隔。
    /// shell：command 是 agent 自己写的（run_shell），不是 skill_run 拼出的 skill 脚本调用（决定能不能注入
    /// allow_shell = false 的 secret）。
    #[allow(clippy::too_many_arguments)]
    pub async fn start(
        self: &Arc<Self>,
        session_id: &str,
        agent_id: &str,
        command: &str,
        secrets: &[String],
        shell: bool,
        awaited: bool,
        check_seconds: f64,
    ) -> Result<JobStarted> {
        // secret 不可用时什么都还没起
        let agent_shell = self.commands.prepare(agent_id, secrets, shell).await?;
        let this = self.clone();
        let (session_id, agent_id, command, secrets) =
            (session_id.to_owned(), agent_id.to_owned(), command.to_owned(), secrets.to_vec());
        // 一气做完：发起它的运行被停止（这个 future 被丢掉）也不会留下没人管的进程或任务行
        let launch = tokio::spawn(async move {
            this.launch(agent_shell, session_id, agent_id, command, secrets, awaited, check_seconds).await
        });
        launch.await.unwrap_or_else(|e| std::panic::resume_unwind(e.into_panic()))
    }

    #[allow(clippy::too_many_arguments)]
    async fn launch(
        self: Arc<Self>,
        agent_shell: AgentShell,
        session_id: String,
        agent_id: String,
        command: String,
        secrets: Vec<String>,
        awaited: bool,
        check_seconds: f64,
    ) -> Result<JobStarted> {
        let workspace = agent_shell.cwd.clone();
        tokio::fs::create_dir_all(workspace.join(JOBS_DIR)).await?;
        // 先起进程（输出管道满了它会等一下，照看者马上接手），再写任务行：没有「有行无进程」的时候
        let spawned = agent_shell.spawn(&command, false, &Options { merge_stderr: true, ..Options::default() })?;
        let pgid = spawned.pgid;
        let work = self.work.clone();
        let (sid, aid, cmd) = (session_id.clone(), agent_id.clone(), command.clone());
        let secrets_json = serde_json::to_string(&secrets).expect("strings serialize");
        let inserted = self
            .db
            .write(move |tx| -> Result<JobStarted> {
                if muliao_sessions::directory::get_in(tx, &sid)?.is_none() {
                    return Err(not_found(format!("session {sid:?} not found")));
                }
                tx.execute(
                    "INSERT INTO jobs (session_id, agent_id, command, secrets, status, log_path, pid, check_seconds, \
                     started_at) VALUES (?1, ?2, ?3, ?4, 'running', '', ?5, ?6, ?7)",
                    params![sid, aid, cmd, secrets_json, pgid, check_seconds, Utc::now()],
                )?;
                let id = tx.last_insert_rowid();
                let log_path = format!("{JOBS_DIR}/{id}.log");
                tx.execute("UPDATE jobs SET log_path = ?1 WHERE id = ?2", params![log_path, id])?;
                // 同一事务里在会话上登记占用（会话据此知道工作还没做完；用户是否在等结局记在占用上）
                work.hold_in(tx, &sid, id, awaited)?;
                Ok(JobStarted { id, log_path })
            })
            .await;
        let started = match inserted {
            Ok(started) => started,
            Err(e) => {
                // 记不下来就不让它跑：没有人能结束它
                kill_group(pgid);
                let mut child = spawned.child;
                tokio::spawn(async move { child.wait().await });
                return Err(e);
            }
        };
        let (done_tx, done) = watch::channel(false);
        let handle = Arc::new(Handle { pgid, process: Mutex::new(Process::Running), done });
        self.running.lock().expect("running lock").insert(started.id, handle.clone());
        let log_file = workspace.join(&started.log_path);
        let (this, id) = (self.clone(), started.id);
        tokio::spawn(async move {
            this.supervise(id, handle, spawned, agent_shell, log_file).await;
            this.running.lock().expect("running lock").remove(&id);
            let _ = done_tx.send(true);
        });
        tracing::info!(job_id = started.id, session_id, pid = pgid, "background job started");
        Ok(started)
    }

    /// 看着进程直到结局写进库。结局写不进去（数据库出错）时记 error，这一行停在 running，下次启动时收掉。
    async fn supervise(
        &self,
        job_id: i64,
        handle: Arc<Handle>,
        spawned: Spawned,
        shell: AgentShell,
        log_file: PathBuf,
    ) {
        let Spawned { mut child, mut stdout, .. } = spawned;
        let (exit_code, drained, recorded, ending) = {
            let mut recording = std::pin::pin!(record_output(&mut stdout, &log_file, &shell.secrets));
            let mut read: Option<std::io::Result<()>> = None;
            let status = loop {
                tokio::select! {
                    r = &mut recording, if read.is_none() => {
                        if r.is_err() {
                            // 读输出、写日志意外失败：进程不能没人管地留着
                            kill_group(handle.pgid);
                        }
                        read = Some(r);
                    }
                    s = child.wait() => break s,
                }
            };
            let ending = handle.reaped();
            if read.is_none() {
                // 以进程退出为准：退出后最多再给 DRAIN 读完（脱离进程组的后代可能一直占着管道）
                read = tokio::time::timeout(DRAIN, &mut recording).await.ok();
            }
            let code = match status {
                Ok(s) => {
                    use std::os::unix::process::ExitStatusExt;
                    Some(s.code().unwrap_or_else(|| -s.signal().unwrap_or(0)))
                }
                Err(e) => {
                    tracing::error!(job_id, error = %e, "waiting for a background job failed; its exit code is unknown");
                    None
                }
            };
            (code, read.is_some(), read.unwrap_or(Ok(())), ending)
        };
        let (end, note) = match (recorded, ending) {
            (Err(e), _) => {
                tracing::error!(job_id, error = %e, "background job output could not be recorded");
                (End::Interrupted, Some("its output could not be recorded (see server log)".to_owned()))
            }
            (Ok(()), Some(Ending::Interrupted(reason))) => (End::Interrupted, Some(reason)),
            (Ok(()), Some(Ending::Killed)) => {
                (End::Killed { code: exit_code }, (!drained).then(|| LEFT_RUNNING_NOTE.to_owned()))
            }
            (Ok(()), None) => (End::Exited { code: exit_code }, (!drained).then(|| LEFT_RUNNING_NOTE.to_owned())),
        };
        if let Err(e) = self.finish(job_id, &log_file, end, note, &shell).await {
            tracing::error!(job_id, error = %e, "background job outcome could not be recorded; it stays running until the next start");
            return;
        }
        tracing::info!(job_id, end = ?end, "background job finished");
    }

    async fn finish(
        &self,
        job_id: i64,
        log_file: &std::path::Path,
        end: End,
        note: Option<String>,
        shell: &AgentShell,
    ) -> Result<()> {
        // 事务里不做文件 IO：进程已经结束，先读好输出的最后几行
        let path = log_file.to_path_buf();
        let last = tokio::task::spawn_blocking(move || log_tail(&path)).await.expect("reading the log panicked");
        let exit_code = match end {
            End::Exited { code } | End::Killed { code } => code,
            End::Interrupted => None,
        };
        let refused = shell.refusal(exit_code, &last);
        let work = self.work.clone();
        self.db
            .write(move |tx| {
                let mut job = job_in(tx, job_id)?.ok_or_else(|| {
                    muliao_platform::errors::broken(format!("job {job_id} vanished while it was running"))
                })?;
                if !job.state.running() {
                    // 结局只记一次：照看者在写完之前一直登记着
                    return Err(muliao_platform::errors::broken(format!(
                        "job {job_id} already ended as {}, cannot record {end:?}",
                        job.state.status().as_str()
                    )));
                }
                let now = Utc::now();
                job.state = end.at(now);
                let text = final_text(&job, end, now, &last, note.as_deref());
                let text = match refused {
                    Some(r) => format!("{text}\n{r}"),
                    None => text,
                };
                record_end_in(tx, &work, &job, &text)
            })
            .await
    }

    /// 结束这个 agent 的一个运行中的任务；结果（killed）随后作为事件到达。
    pub async fn kill(&self, job_id: i64, agent_id: &str) -> Result<()> {
        let agent = agent_id.to_owned();
        let job = self.db.read(move |c| own_in(c, job_id, &agent)).await?;
        if !job.state.running() {
            return Err(conflict(format!("job {job_id} is not running (status: {})", job.state.status().as_str())));
        }
        let handle = self.running.lock().expect("running lock").get(&job_id).cloned();
        if !handle.is_some_and(|h| h.end(Ending::Killed)) {
            // 进程刚好自己退出了，结局正在记下
            return Err(conflict(format!("job {job_id} has just ended; its final status will arrive as an event")));
        }
        tracing::info!(job_id, "background job kill requested");
        Ok(())
    }

    /// 会话被用户停止（订阅会话领域的事件，在停止的事务里）：提交之后结束被清掉的占用对应的后台任务（同 job_kill），结局
    /// 照常由照看者记下（killed）；会话的占用已经清掉，结局不再投信。
    pub fn on_stopped(self: &Arc<Self>, tx: &mut Tx<'_>, event: &SessionStopped) {
        let jobs: Vec<i64> = event
            .cleared
            .iter()
            .filter_map(|h| match h {
                HoldOf::Job { job_id } => Some(*job_id),
                _ => None,
            })
            .collect();
        if jobs.is_empty() {
            return;
        }
        let (this, session_id) = (self.clone(), event.session_id.clone());
        tx.on_commit(move || {
            let running = this.running.lock().expect("running lock");
            let stopped: Vec<i64> =
                jobs.into_iter().filter(|id| running.get(id).is_some_and(|h| h.end(Ending::Killed))).collect();
            if !stopped.is_empty() {
                tracing::info!(session_id, job_ids = ?stopped, "background jobs stopped with their session");
            }
        });
    }

    pub async fn info(&self, job_id: i64, agent_id: &str) -> Result<JobInfo> {
        let agent = agent_id.to_owned();
        let job = self.db.read(move |c| own_in(c, job_id, &agent)).await?;
        let path = self.log_file(&job);
        let tail = tokio::task::spawn_blocking(move || log_tail(&path)).await.expect("reading the log panicked");
        Ok(JobInfo {
            id: job.id,
            command: job.command.clone(),
            state: job.state,
            log_path: job.log_path.clone(),
            running_for: duration(
                job.started_at,
                match job.state {
                    JobState::Running => Utc::now(),
                    JobState::Ended { at, .. } => at,
                },
            ),
            tail,
        })
    }

    async fn tails(&self, jobs: &[Job]) -> BTreeMap<i64, String> {
        let files: Vec<(i64, PathBuf)> = jobs.iter().map(|j| (j.id, self.log_file(j))).collect();
        tokio::task::spawn_blocking(move || files.into_iter().map(|(id, path)| (id, log_tail(&path))).collect())
            .await
            .expect("reading logs panicked")
    }

    /// 给到了自己检查间隔的运行中任务各投一封检查信件（会话领域投；它上一封还在信箱里排着时不投，不堆积）。返回发出的
    /// 封数。只检查有人照看的任务。
    pub async fn check(&self) -> Result<usize> {
        let supervised: BTreeSet<i64> = self.running.lock().expect("running lock").keys().copied().collect();
        self.beats.lock().expect("beats lock").keep(&supervised);
        if supervised.is_empty() {
            return Ok(0);
        }
        let filter = format!(
            "status = 'running' AND id IN ({})",
            supervised.iter().map(|id| id.to_string()).collect::<Vec<_>>().join(", ")
        );
        let candidates = self.db.read(move |c| jobs_where(c, &filter, [])).await?;
        let now = Utc::now();
        let due: Vec<Job> = {
            let beats = self.beats.lock().expect("beats lock");
            candidates.into_iter().filter(|j| beats.due(&j.id, j.started_at, j.check_seconds, now)).collect()
        };
        if due.is_empty() {
            return Ok(0);
        }
        let tails = self.tails(&due).await;
        let work = self.work.clone();
        let ids: Vec<i64> = due.iter().map(|j| j.id).collect();
        // 在确认任务仍在运行的同一个事务里投检查信件：与记下结局（同一事务交回结局）互斥，检查要么先于结束，要么任务已经
        // 结束、不再发
        // 节拍按「到了就算检查过」前移（上一封还排着没投的也算），返回的是真投出的封数
        let (checked, sent) = self
            .db
            .write(move |tx| {
                let (mut checked, mut sent) = (Vec::new(), 0);
                for id in ids {
                    let Some(job) = job_in(tx, id)?.filter(|j| j.state.running()) else { continue };
                    let text = check_text(&job, Utc::now(), &tails[&id]);
                    if work.checked_in(tx, &job.session_id, id, &text)? {
                        sent += 1;
                    }
                    checked.push(id);
                }
                Ok::<_, Error>((checked, sent))
            })
            .await?;
        let mut beats = self.beats.lock().expect("beats lock");
        for id in checked {
            beats.checked(id, now);
        }
        Ok(sent)
    }

    /// 组合根在服务启动时、对外服务（起任何后台任务）之前调用一次：先收掉上次服务没能收尾的任务（[`JobRunner::recover`]），
    /// 再起常驻的运行中检查（每个节拍一次）。两件事只在这里一起做，「先 recover」不靠调用方记。
    pub async fn run(self: &Arc<Self>) -> Result<tokio::task::JoinHandle<()>> {
        self.recover().await?;
        let this = self.clone();
        Ok(tokio::spawn(async move {
            loop {
                tokio::time::sleep(TICK).await;
                if let Err(e) = this.check().await {
                    tracing::error!(error = %e, "checking background jobs failed");
                }
            }
        }))
    }

    /// 服务启动时、还没有任务在跑时调用一次：这时记为运行中的是上次服务没能收尾的（被强杀、崩溃，或照看者没写下结局）。
    /// 进程自成进程组，不随服务退出，可能还活着：如实告诉 agent，不盲杀（pid 可能已被复用）。
    async fn recover(&self) -> Result<usize> {
        let stale = self.db.read(|c| jobs_where(c, "status = 'running'", [])).await?;
        if stale.is_empty() {
            return Ok(0);
        }
        let tails = self.tails(&stale).await;
        let work = self.work.clone();
        let ids: Vec<i64> = stale.iter().map(|j| j.id).collect();
        let closed = self
            .db
            .write(move |tx| {
                let mut closed = Vec::new();
                for id in ids {
                    let Some(mut job) = job_in(tx, id)?.filter(|j| j.state.running()) else { continue };
                    let now = Utc::now();
                    job.state = End::Interrupted.at(now);
                    let mut note = "the service stopped unexpectedly".to_owned();
                    if let Some(pid) = job.pid {
                        note.push_str(&format!(
                            ". Its process group {pid} may still be running: check with `pgrep -g {pid}` and stop it \
                             with `kill -- -{pid}` if it should not keep running"
                        ));
                    }
                    let text = final_text(&job, End::Interrupted, now, &tails[&id], Some(&note));
                    record_end_in(tx, &work, &job, &text)?;
                    closed.push(id);
                }
                Ok::<_, Error>(closed)
            })
            .await?;
        if !closed.is_empty() {
            tracing::warn!(job_ids = ?closed, "background jobs left by an unexpected stop marked interrupted");
        }
        Ok(closed.len())
    }

    /// 正常关停：杀掉所有后台任务的进程组，由照看者记为被中断并告诉 agent（事件在重启后处理）。等照看者写完最多
    /// [`SHUTDOWN_WAIT`]；没写完的下次启动时收掉。
    pub async fn shutdown(&self) {
        let handles: Vec<(i64, Arc<Handle>)> =
            self.running.lock().expect("running lock").iter().map(|(id, h)| (*id, h.clone())).collect();
        for (_, h) in &handles {
            // 进程已经自己结束、正在记结局的：给它时间写完，保留真实的退出码
            h.end(Ending::Interrupted("the service was stopped".to_owned()));
        }
        let wait = async {
            for (_, h) in &handles {
                let mut done = h.done.clone();
                let _ = done.wait_for(|d| *d).await;
            }
        };
        if tokio::time::timeout(SHUTDOWN_WAIT, wait).await.is_err() {
            let left: Vec<i64> = handles.iter().filter(|(_, h)| !*h.done.borrow()).map(|(id, _)| *id).collect();
            tracing::warn!(job_ids = ?left, "background jobs did not record their outcome before shutdown");
        }
        if !handles.is_empty() {
            tracing::info!(job_ids = ?handles.iter().map(|(id, _)| *id).collect::<Vec<_>>(), "background jobs stopped with the service");
        }
    }
}

/// 记下结局并交回会话（同一个事务）：会话投一封事件信件并释放占用；会话已被停止（占用已清掉）或已删除时不投。
fn record_end_in(tx: &mut Tx<'_>, work: &SessionWork, job: &Job, text: &str) -> Result<()> {
    let (status, exit_code, finished_at) = job.state.columns();
    tx.execute(
        "UPDATE jobs SET status = ?1, exit_code = ?2, finished_at = ?3 WHERE id = ?4",
        params![status, exit_code, finished_at, job.id],
    )?;
    work.ended_in(tx, &job.session_id, job.id, text)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_print_like_python_g() {
        for (x, s) in [
            (60.0, "60"),
            (0.5, "0.5"),
            (300.0, "300"),
            (1e-5, "1e-05"),
            (1234567.0, "1.23457e+06"),
            (0.1234567, "0.123457"),
            (123456.0, "123456"),
            (1.5, "1.5"),
        ] {
            assert_eq!(format_g(x), s, "{x}");
        }
    }

    #[test]
    fn durations_like_python() {
        let t = |s: i64| DateTime::<Utc>::from_timestamp(s, 0).unwrap();
        assert_eq!(duration(t(0), t(5)), "0m05s");
        assert_eq!(duration(t(0), t(3725)), "1h02m05s");
    }
}
