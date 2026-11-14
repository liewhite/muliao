//! 会话领域的工具：ask_user（向用户提问）、委派（delegate_task、ask_agent、stop_thread、list_threads）、wake_me（到点唤醒）。
//! 都只经会话领域的接口做事；挂起的调用（ask_user、ask_agent）交回 `ToolError::Defer`，运行停在这里，回答作为结果交回
//! （[`crate::pending`]）。

use muliao_platform::ids::{CallId, SessionId};
use std::sync::{Arc, OnceLock};

use agenter::{Tool, ToolCtx, ToolError, ToolOutput};
use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use muliao_assets::agents::{agent_names_in, named_in};
use muliao_platform::db::Database;
use muliao_platform::text::truncate;
use muliao_platform::time::parse_local;
use muliao_platform::tooling::{Audience, BuiltinTool, RunScope, ToolTraits, builtin};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::activity::WorkState;
use crate::actor::{Actors, Waker};
use crate::answers::AskArgs;
use crate::delegation::{Mode, send_in, threads_in};
use crate::halt::Halted;
use crate::origins::ThreadName;
use crate::paused_calls::{self, Pause};
use crate::thread_calls::Delegated;
use crate::wakeups::{WAKE_ME, cancel_in, local_time, schedule_in};
use crate::work::{check_interval, no_progress_hint};
use muliao_platform::errors::{Error, Result, invalid, not_found};

/// 停止会话的入口，在 actor 系统起来之后接上（工具先于 actor 系统构造：工具 → 运行端口 → actor 系统）。
#[derive(Clone, Default)]
pub struct Stopper(Arc<OnceLock<Actors>>);

impl Stopper {
    pub fn bind(&self, actors: Actors) {
        assert!(self.0.set(actors).is_ok(), "the session stopper is bound once");
    }

    pub async fn stop(&self, session_id: &SessionId) -> Result<Halted> {
        self.0.get().expect("session actors are started before any run").stop(session_id).await
    }
}

pub(crate) const ASK_USER: &str = "ask_user";
const ASK_AGENT: &str = "ask_agent";

/// 会话领域的工具组。
pub fn tools(db: Arc<Database>, waker: Waker, stopper: Stopper, tz: Tz) -> Vec<BuiltinTool> {
    vec![
        builtin(AskUser { db: db.clone() }, ToolTraits::read()),
        builtin(
            DelegateTask { db: db.clone(), waker: waker.clone(), description: delegate_description() },
            ToolTraits::write(),
        ),
        builtin(AskAgent { db: db.clone(), waker }, ToolTraits::write()),
        builtin(StopThread { db: db.clone(), stopper }, ToolTraits::write()),
        builtin(ListThreads { db: db.clone() }, ToolTraits::read()),
        builtin(WakeMe { db, tz }, ToolTraits::write()),
    ]
}

fn call_id<C>(ctx: &ToolCtx<'_, C>) -> CallId {
    CallId::new(ctx.call.id.clone())
}

// ---- ask_user ----

const NO_USER_WAITING: &str = "No user is waiting for this run's answer, so nobody can answer questions here. Decide \
     yourself, or send the user a notification (`notify_user`, if you have it) if they must know something, and carry on.";
const ASKED_BY_AGENT: &str = "Another agent asked you this and is waiting for your final answer; the user is not here \
     to answer questions. Decide yourself, or put what that agent must decide into your final answer; it can reply in \
     this same thread.";

pub struct AskUser {
    db: Arc<Database>,
}

impl Tool<RunScope> for AskUser {
    type Args = AskArgs;

    fn name(&self) -> &str {
        ASK_USER
    }

    fn description(&self) -> &str {
        "Ask the user one to four multiple-choice questions when you need their decision or preference to continue, \
         e.g. which option to pursue or which constraint matters. They can always answer \"other\" in their own \
         words, so do not add an \"Other\" option. Put all questions in one call. Your turn ends once this response's \
         tool calls finish; the answers arrive as this tool's result when the user replies. Don't ask what you can \
         reasonably decide or look up yourself. Only works when the run context says the user is notified with your \
         final answer."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, args: AskArgs) -> std::result::Result<ToolOutput, ToolError> {
        args.check().map_err(ToolError::Retry)?;
        match ctx.deps.audience {
            Audience::User => {
                // 挂起之前记下：之后只凭这一行认它在等回答
                let (session_id, call) = (ctx.deps.session_id.clone(), call_id(&ctx));
                self.db.write(move |tx| paused_calls::record_in(tx, &session_id, &call, &Pause::AskUser)).await?;
                Err(ToolError::Defer)
            }
            Audience::Agent => Err(ToolError::Retry(ASKED_BY_AGENT.to_owned())),
            Audience::None => Err(ToolError::Retry(NO_USER_WAITING.to_owned())),
        }
    }
}

// ---- 委派 ----

#[derive(Deserialize, JsonSchema)]
pub struct DelegateArgs {
    /// Target agent name from `list_agents`, or your own name.
    agent: String,
    /// Self-contained task description; the agent cannot see this conversation except through earlier messages in
    /// the same thread.
    task: String,
    /// Short name of the thread, e.g. "weekly-report". Reusing a thread name with the same agent continues that
    /// conversation with its full context, whether it was opened with `delegate_task` or `ask_agent`; use a new name
    /// for an unrelated task.
    thread: ThreadName,
    /// Get the thread's answer back as an event in this conversation.
    #[serde(default)]
    report_back: bool,
    /// With `report_back: true` only: seconds between check-in events while it works (> 0, default 60); choose it
    /// from how long you expect the task to take, e.g. 600 for an hour-long task.
    #[serde(default)]
    heartbeat_seconds: Option<f64>,
}

pub struct DelegateTask {
    db: Arc<Database>,
    waker: Waker,
    description: String,
}

fn delegate_description() -> String {
    format!(
        "Hand a task to an agent and keep going without waiting (non-blocking, no timeout). It runs in a thread the \
         user can see and reply in. By default the user is notified with its answer and you do NOT get it back. With \
         `report_back: true` the answer (or its failure) comes back to you as an event in this conversation instead \
         (events reach you like messages, between your tool calls if you are busy), and while it is still working you \
         get a check-in event every `heartbeat_seconds` (default 60) saying what the thread is doing: then either keep \
         waiting ({}; `list_threads` shows its latest reply) or stop it with `stop_thread`. If you need the answer \
         before you can continue, use `ask_agent`. The agent can be yourself (your own name): a fresh conversation of \
         you, with your memory, skills and tools, for work to run on the side.",
        no_progress_hint()
    )
}

impl Tool<RunScope> for DelegateTask {
    type Args = DelegateArgs;

    fn name(&self) -> &str {
        "delegate_task"
    }

    fn description(&self) -> &str {
        &self.description
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: DelegateArgs) -> std::result::Result<ToolOutput, ToolError> {
        if a.heartbeat_seconds.is_some() && !a.report_back {
            return Err(ToolError::Retry(
                "heartbeat_seconds needs `report_back: true`; without it the result goes to the user, not to you"
                    .into(),
            ));
        }
        let check = check_interval(a.heartbeat_seconds).map_err(ToolError::Retry)?;
        let thread = a.thread;
        let (parent, call, waker, awaited) =
            (ctx.deps.session_id.clone(), call_id(&ctx), self.waker.clone(), ctx.deps.user_waits());
        let (agent, task, report_back, name) = (a.agent, a.task, a.report_back, thread.clone());
        let target = self
            .db
            .write(move |tx| {
                let target = named_in(tx, &agent)?;
                let mode = if report_back {
                    Mode::Delegated { kind: Delegated::ReportBack { check_seconds: check }, awaited }
                } else {
                    Mode::Plain
                };
                send_in(tx, &waker, &parent, &target.id, &name, &task, &call, mode)?;
                Ok::<_, Error>(target.name)
            })
            .await?;
        Ok(agenter::tool::text(format!("delegated to {target} in thread {thread:?}")))
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct AskAgentArgs {
    /// Target agent name from `list_agents`, or your own name.
    agent: String,
    /// Self-contained request; the agent sees only this thread's history.
    message: String,
    /// Short name of the thread, e.g. "code-review". Reuse it with the same agent to continue the same conversation
    /// (also one opened with `delegate_task`).
    thread: ThreadName,
}

pub struct AskAgent {
    db: Arc<Database>,
    waker: Waker,
}

impl Tool<RunScope> for AskAgent {
    type Args = AskAgentArgs;

    fn name(&self) -> &str {
        ASK_AGENT
    }

    fn description(&self) -> &str {
        "Ask an agent and wait for its answer, which is returned as this tool's result. This is blocking: your reply \
         pauses until the answer arrives (there is no timeout; the agent's run always ends with an answer, a failure or \
         a stop). The user can see the thread and reply in it. Files it shares are shown to the user with your reply. \
         Ask again in the same thread (same agent and name) to follow up with the agent's context intact, e.g. review \
         -> fix -> review again; this also continues a thread opened with `delegate_task`. Several asks in one response \
         run in parallel; asks to the same thread share it. If the user writes to you meanwhile, you continue and the \
         outcome reaches you later as an event. For long work you want to keep an eye on while doing other things, use \
         `delegate_task` with `report_back: true` instead. The agent can be yourself (your own name): a fresh \
         conversation of you, with your memory, skills and tools, for a self-contained side job (e.g. a code review, \
         or several parts of a task in parallel) whose context you do not want in this conversation."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: AskAgentArgs) -> std::result::Result<ToolOutput, ToolError> {
        if a.message.trim().is_empty() {
            return Err(ToolError::Retry("message must not be empty".into()));
        }
        let thread = a.thread;
        let (parent, call, waker, awaited) =
            (ctx.deps.session_id.clone(), call_id(&ctx), self.waker.clone(), ctx.deps.user_waits());
        let (agent, message) = (a.agent, a.message);
        self.db
            .write(move |tx| {
                let target = named_in(tx, &agent)?.id;
                send_in(
                    tx,
                    &waker,
                    &parent,
                    &target,
                    &thread,
                    &message,
                    &call,
                    Mode::Delegated { kind: Delegated::Ask, awaited },
                )
            })
            .await?;
        // 发问方的运行停在这次调用上；对方的结局作为这次调用的结果交回
        Err(ToolError::Defer)
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct StopThreadArgs {
    /// The thread name used with `delegate_task` or `ask_agent`.
    thread: ThreadName,
    /// The thread's agent. Threads are told apart by agent and name: you may leave it out unless you opened threads
    /// with this name for several agents.
    #[serde(default)]
    agent: Option<String>,
}

pub struct StopThread {
    db: Arc<Database>,
    stopper: Stopper,
}

impl Tool<RunScope> for StopThread {
    type Args = StopThreadArgs;

    fn name(&self) -> &str {
        "stop_thread"
    }

    fn description(&self) -> &str {
        "Stop a thread of this conversation, whether it is running or still queued: its current run stops (what it \
         already did is kept) and the work queued for it is dropped. Use it when a check-in or `list_threads` shows the \
         work is stuck or no longer needed. If you are waiting for its answer (`ask_agent`, or `report_back`), you get \
         its outcome saying it was stopped, with what it said so far."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: StopThreadArgs) -> std::result::Result<ToolOutput, ToolError> {
        let (parent, wanted) = (ctx.deps.session_id.clone(), a.thread.clone());
        let (child, who) = self
            .db
            .read(move |c| {
                let mut named = crate::directory::threads_named_in(c, &parent, &wanted)?;
                if let Some(agent) = &a.agent {
                    let id = named_in(c, agent)?.id;
                    named.retain(|s| s.agent_id == id);
                }
                let names = agent_names_in(c)?;
                match named.as_slice() {
                    [] => {
                        let whose = a.agent.as_ref().map(|x| format!(" with {x}")).unwrap_or_default();
                        Err(not_found(format!(
                            "this conversation has no thread named {wanted:?}{whose}; see list_threads"
                        )))
                    }
                    [one] => Ok((one.id.clone(), format!("thread {wanted:?} ({})", names.name(&one.agent_id)))),
                    many => Err(invalid(format!(
                        "several threads are named {wanted:?} (agents: {}); pass `agent` to say which one",
                        many.iter().map(|s| names.name(&s.agent_id)).collect::<Vec<_>>().join(", ")
                    ))),
                }
            })
            .await?;
        let text = match self.stopper.stop(&child).await? {
            // 查到之后、停止之前被删除了（随所属 agent 删除）
            Halted::Gone => format!("{who} was deleted meanwhile; nothing to stop"),
            Halted::Stopped => format!("stopped {who}"),
        };
        Ok(agenter::tool::text(text))
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct NoArgs {}

#[derive(Serialize)]
struct ThreadInfo {
    thread: ThreadName,
    agent: String,
    state: WorkState,
    failure: Option<String>,
    answer: Option<String>,
}

pub struct ListThreads {
    db: Arc<Database>,
}

impl Tool<RunScope> for ListThreads {
    type Args = NoArgs;

    fn name(&self) -> &str {
        "list_threads"
    }

    fn description(&self) -> &str {
        "List the threads this conversation has opened (each is one agent + name). Returns a JSON array of threads, \
         each with `thread`, `agent`, `state`, `failure` and `answer`. Use it to look at delegated work, e.g. after a \
         check-in event, before deciding to keep waiting or `stop_thread`. `state`: running (working now), waiting \
         (its run ended but it is waiting for work it started, a wake-up or answers it asked for: not finished yet), \
         idle (nothing in progress). `answer` is its latest reply; while it is not idle that reply is intermediate, \
         not its result. `failure` is set when its latest run failed for good."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, _a: NoArgs) -> std::result::Result<ToolOutput, ToolError> {
        let parent = ctx.deps.session_id.clone();
        let threads = self
            .db
            .read(move |c| {
                // 包括正在删除的 agent（它的 thread 在删除流程里随后删除）
                let names = agent_names_in(c)?;
                Ok::<_, Error>(
                    threads_in(c, &parent)?
                        .into_iter()
                        .map(|v| ThreadInfo {
                            agent: names.name(&v.agent_id),
                            thread: v.thread,
                            state: v.state,
                            failure: v.failure,
                            answer: (!v.answer.is_empty()).then(|| truncate(&v.answer, 500)),
                        })
                        .collect::<Vec<_>>(),
                )
            })
            .await?;
        Ok(agenter::tool::json(&threads))
    }
}

// ---- wake_me ----

#[derive(Deserialize, JsonSchema)]
pub struct WakeArgs {
    /// Required when scheduling: what to do when woken (you will see only this note then).
    #[serde(default)]
    note: Option<String>,
    /// When to wake up, ISO 8601, e.g. "2026-10-22T10:30"; without an offset it is local time.
    #[serde(default)]
    at: Option<String>,
    /// Instead of `at`: seconds from now (> 0, no upper limit).
    #[serde(default)]
    after_seconds: Option<f64>,
    /// Remove the scheduled wake-up instead of scheduling one (give nothing else).
    #[serde(default)]
    cancel: bool,
}

pub struct WakeMe {
    db: Arc<Database>,
    tz: Tz,
}

/// 约的时刻：`at`（不带时区的按服务端时区解释，同 once 触发器）或 `after_seconds`，二选一，必须在将来。
fn due(
    at: Option<&str>,
    after_seconds: Option<f64>,
    tz: Tz,
    now: DateTime<Utc>,
) -> std::result::Result<DateTime<Utc>, String> {
    match (at, after_seconds) {
        (Some(_), Some(_)) | (None, None) => {
            Err("give exactly one of `at` and `after_seconds` (or `cancel: true` alone)".into())
        }
        (None, Some(s)) => {
            if s.is_nan() || s <= 0.0 {
                return Err("after_seconds must be greater than 0".into());
            }
            chrono::Duration::try_milliseconds((s * 1000.0) as i64)
                .and_then(|d| now.checked_add_signed(d))
                .ok_or_else(|| format!("after_seconds {s} is too far in the future"))
        }
        (Some(at), None) => {
            let due = parse_local(at, tz)
                .ok_or_else(|| format!("`at` must be an ISO 8601 time such as \"2026-10-22T10:30\", not {at:?}"))?;
            if due <= now {
                return Err(format!("`at` {at:?} is in the past (now is {})", local_time(now, tz)));
            }
            Ok(due)
        }
    }
}

impl Tool<RunScope> for WakeMe {
    type Args = WakeArgs;

    fn name(&self) -> &str {
        WAKE_ME
    }

    fn description(&self) -> &str {
        "Wake this conversation up later: when the time comes, your `note` arrives here as an event and you continue. \
         Until then the conversation is waiting (like on a background job or delegated work): it is not idle, so an \
         issue it works on is not stalled and you are not asked to settle it. Use it when you can only check now and \
         then (a deploy settling, a reply on an external platform, a result due later); when a script can poll for the \
         condition, run the polling loop with `run_shell(background: true)` instead, so it ends as soon as the \
         condition is met. A conversation has at most one wake-up: scheduling again replaces it, `cancel: true` removes \
         it, and the user stopping the conversation cancels it. After scheduling, end your turn."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: WakeArgs) -> std::result::Result<ToolOutput, ToolError> {
        let (session_id, tz) = (ctx.deps.session_id.clone(), self.tz);
        if a.cancel {
            if a.note.is_some() || a.at.is_some() || a.after_seconds.is_some() {
                return Err(ToolError::Retry("`cancel: true` takes no other arguments".into()));
            }
            let cancelled = self.db.write(move |tx| cancel_in(tx, &session_id)).await?;
            return Ok(agenter::tool::text(match cancelled {
                None => "no wake-up was scheduled; nothing to cancel".to_owned(),
                Some(at) => format!("cancelled wake-up at {}", local_time(at, tz)),
            }));
        }
        let due_at = due(a.at.as_deref(), a.after_seconds, tz, Utc::now()).map_err(ToolError::Retry)?;
        let note = a
            .note
            .filter(|n| !n.trim().is_empty())
            .ok_or_else(|| ToolError::Retry("`note` is required: say what to do when woken".into()))?;
        let awaited = ctx.deps.user_waits();
        let scheduled = self.db.write(move |tx| schedule_in(tx, &session_id, due_at, &note, awaited)).await?;
        let mut text = format!("scheduled wake-up at {}", local_time(scheduled.due_at, tz));
        if let Some(r) = scheduled.replaced {
            text.push_str(&format!("; replaced the one at {}", local_time(r, tz)));
        }
        Ok(agenter::tool::text(text))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn wake_times() {
        let tz: Tz = "Asia/Shanghai".parse().unwrap();
        let now = Utc.with_ymd_and_hms(2026, 11, 12, 0, 0, 0).unwrap();
        assert_eq!(due(None, Some(60.0), tz, now).unwrap(), now + chrono::Duration::seconds(60));
        assert!(due(None, Some(0.0), tz, now).is_err());
        assert!(due(None, Some(f64::NAN), tz, now).is_err());
        assert!(due(Some("x"), None, tz, now).is_err());
        assert!(due(Some("2026-11-12T10:30"), Some(1.0), tz, now).is_err());
        // 不带时区按本地时间：上海 10:30 是 UTC 02:30
        assert_eq!(
            due(Some("2026-11-12T10:30"), None, tz, now).unwrap(),
            Utc.with_ymd_and_hms(2026, 11, 12, 2, 30, 0).unwrap()
        );
        assert!(due(Some("2026-11-12T07:00"), None, tz, now).is_err(), "in the past");
        assert_eq!(
            due(Some("2026-11-12T10:30:00+00:00"), None, tz, now).unwrap(),
            Utc.with_ymd_and_hms(2026, 11, 12, 10, 30, 0).unwrap()
        );
        assert_eq!(
            due(Some("2026-11-12T10:30Z"), None, tz, now).unwrap(),
            Utc.with_ymd_and_hms(2026, 11, 12, 10, 30, 0).unwrap()
        );
        // 带时区不带秒：只按它自己的时区算一次
        assert_eq!(
            due(Some("2026-11-12T10:30+08:00"), None, tz, now).unwrap(),
            Utc.with_ymd_and_hms(2026, 11, 12, 2, 30, 0).unwrap()
        );
    }
}
