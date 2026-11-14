//! thread 与委派（docs/architecture.md 第 7、8 节）。
//!
//! - **thread**：父会话内对子会话的具名引用，由（父会话, agent, 名字）确定，同名即续聊（不论用哪个工具开的）。
//! - **委派**：要交回结局的工作（ask_agent、delegate_task(report_back)）。整个生命周期是两个对称的事务，唯一主体是发出它的
//!   调用记下的那一行（[`crate::thread_calls`]）：
//!   - 发出（[`dispatch_in`]）：打开或找到 thread，记下委派行（按发起会话与调用 id 幂等），在发起方登记引用它的占用，往
//!     thread 投一封引用它的信件。
//!   - 交回（[`hand_back_in`]，thread 结清时调用：做完、最终失败、被停止、被删除）：同一个事务里经 `deliver_in` 把结局交给
//!     发起方并释放占用（发起方还停在那次 ask_agent 上时作为调用的结果，否则作为事件）。只交回占用还 active 的。
//! - 普通 delegate_task（[`send_in`]）不交回结局：只往 thread 投一封用户在等的信。
//! - 运行中检查（[`check_threads`]）：report_back 的委派按 heartbeat_seconds 给发起方投检查信件；ask_agent 不设超时。

use muliao_platform::ids::{AgentId, DelegationId, SessionId};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use chrono::{DateTime, Utc};
use muliao_assets::agents::{AgentInfo, agent_in};
use muliao_platform::db::{Database, Tx};
use muliao_platform::text::truncate;
use rusqlite::Connection;

use crate::activity::{WorkState, work_states_in};
use crate::actor::Waker;
use crate::answers::{AgentAnswer, CallResult};
use crate::directory::{self, Session};
use crate::history::latest_answers_among;
use crate::holds::hold_delegation_in;
use crate::inbound::{Handback, Inbound, Opener, create_session_in, receive_in};
use crate::paused_calls::{self, Pause};
use crate::runs::{RunRecord, latest_among};
use crate::thread_calls::{CallMode, Delegated, Owed, owed_in, record_call_in};
use crate::work::{CheckBeats, EventSource, deliver_in, no_progress_hint, post_check_in};
use muliao_platform::errors::{Result, invalid};

pub const THREAD_MAX: usize = 100;

pub(crate) fn validate_thread(name: &str) -> Result<String> {
    let name = name.trim();
    if name.is_empty() || name.chars().count() > THREAD_MAX {
        return Err(invalid(format!("thread name must be 1-{THREAD_MAX} characters")));
    }
    Ok(name.to_owned())
}

pub(crate) fn thread_name(child: &Session) -> &str {
    child.origin.thread().unwrap_or_else(|| panic!("session {} is a thread without a name", child.id))
}

/// 取已有 thread 或新建。
pub(crate) fn open_in(c: &Connection, parent_id: &SessionId, agent_id: &AgentId, thread: &str) -> Result<Session> {
    let thread = validate_thread(thread)?;
    if let Some(existing) = directory::thread_in(c, parent_id, agent_id, &thread)? {
        return Ok(existing);
    }
    create_session_in(c, Opener::Thread { parent: parent_id, name: &thread }, agent_id, &thread)
}

/// 一次往 thread 投信的调用是哪一种。
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) enum Mode {
    /// delegate_task：不交回结局，thread 的回答推给用户（用户在等 thread）
    Plain,
    /// 一项委派（report_back 或 ask_agent）。awaited：用户是否在等发起方的回答（交回的结局随之推送）
    Delegated { kind: Delegated, awaited: bool },
}

/// 往 thread 投信（见模块说明）：打开或找到 thread，记下是这次调用开出或续写了它；要交回结局的再在发起方登记引用它的
/// 占用（这就是一项委派），信件引用它。ask_agent 同一事务里记为挂起。同一次调用不会再执行（被打断的调用补「被中断」、
/// 不重做），调用行上的唯一索引挡住重复。
#[allow(clippy::too_many_arguments)]
pub(crate) fn send_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    parent_id: &SessionId,
    agent_id: &AgentId,
    thread: &str,
    prompt: &str,
    call_id: &str,
    mode: Mode,
) -> Result<Session> {
    let child = open_in(tx, parent_id, agent_id, thread)?;
    let recorded = match mode {
        Mode::Plain => CallMode::Plain,
        Mode::Delegated { kind, .. } => CallMode::Delegated(kind),
    };
    let call = record_call_in(tx, parent_id, call_id, &child.id, recorded)?;
    let inbound = match mode {
        Mode::Plain => Inbound::Delegated {
            parent: parent_id.clone(),
            thread: child.id.clone(),
            content: prompt.to_owned(),
            handback: Handback::User,
        },
        Mode::Delegated { kind, awaited } => {
            hold_delegation_in(tx, parent_id, call.id, awaited)?;
            if kind == Delegated::Ask {
                // 和投信同一个事务：thread 可能在发问方这一步保存完之前就答完，交回时要认得出这是挂起调用的回答
                paused_calls::record_in(tx, parent_id, call_id, &Pause::AskAgent)?;
            }
            tracing::info!(session_id = %parent_id, thread_session_id = %child.id, delegation = %call.id, call_id,
                "delegation dispatched");
            // 没有人等 thread 的回答：结局交回发起方，由它处理
            Inbound::Delegated {
                parent: parent_id.clone(),
                thread: child.id.clone(),
                content: prompt.to_owned(),
                handback: Handback::Parent(call.id),
            }
        }
    };
    receive_in(tx, waker, inbound)?;
    Ok(child)
}

/// 委派来的工作的结局。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ending {
    Finished,
    Stopped,
    Failed,
    Deleted,
}

pub(crate) fn event_text(thread: &str, agent: &str, ending: Ending, text: &str) -> String {
    let who = format!("Thread {thread:?} ({agent})");
    match ending {
        Ending::Finished => format!("{who} finished your task. Its answer:\n{text}"),
        Ending::Stopped => format!(
            "{who} was stopped before it finished. What it said so far:\n{}",
            if text.is_empty() { "(nothing)" } else { text }
        ),
        Ending::Failed => format!("{who} failed: {text}\nYou can ask it again, or delegate to another agent."),
        Ending::Deleted => format!("{who} was deleted by the user before it finished."),
    }
}

/// thread 结清了这些还没结清的委派：在同一个事务里把结局经发起方引用它们的占用交回（交回即释放、即结清）。
pub(crate) fn hand_back_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    child: &Session,
    owed: &[Owed],
    ending: Ending,
    text: &str,
    attachments: &[muliao_runtime::conventions::SharedFile],
) -> Result<()> {
    if owed.is_empty() {
        return Ok(());
    }
    let name = thread_name(child);
    let agent = agent_in(tx, &child.agent_id)?;
    for o in owed {
        // 发起方还停在这次调用上（ask_agent）时作为它的结果：完成时就是回答本身（发问方问的就是它）
        let result_text = if ending == Ending::Finished {
            text.to_owned()
        } else {
            event_text(name, agent.name.as_str(), ending, text)
        };
        let answer = CallResult::AskAgent(AgentAnswer { text: result_text, attachments: attachments.to_vec() });
        let delivered = deliver_in(
            tx,
            waker,
            &o.hold,
            &EventSource::Thread { session_id: child.id.clone() },
            &event_text(name, agent.name.as_str(), ending, text),
            Some((o.delegation.call_id.as_str(), answer)),
        )?;
        tracing::info!(delegation = %o.delegation.id, session_id = %o.delegation.parent_id, thread_session_id = %child.id,
            ending = ?ending, delivered_as = ?delivered, "delegation handed back");
    }
    Ok(())
}

/// 父会话开出的一个 thread（list_threads 看到的）。
#[derive(Debug, Clone, PartialEq)]
pub struct ThreadView {
    pub session_id: SessionId,
    pub thread: String,
    pub agent_id: AgentId,
    pub state: WorkState,
    /// 最近一次运行最终失败时的说明
    pub failure: Option<String>,
    /// 最近的回答（还没有回复时为空）；state 不是 idle 时是中间的回答
    pub answer: String,
}

pub(crate) fn threads_in(c: &Connection, parent_id: &SessionId) -> Result<Vec<ThreadView>> {
    let children = directory::threads_of(c, parent_id)?;
    let ids: Vec<SessionId> = children.iter().map(|t| t.session.id.clone()).collect();
    let states = work_states_in(c, &ids)?;
    let runs = latest_among(c, &ids)?;
    let mut answers = latest_answers_among(c, &ids)?;
    Ok(children
        .into_iter()
        .map(|directory::Thread { session: child, name }| ThreadView {
            thread: name,
            agent_id: child.agent_id.clone(),
            state: states[&child.id],
            failure: runs.get(&child.id).and_then(RunRecord::failure).map(str::to_owned),
            answer: answers.remove(&child.id).unwrap_or_default(),
            session_id: child.id,
        })
        .collect())
}

/// 委派的运行中检查：只看还没结清、report_back 的委派，每过一个间隔给发起方投一封检查信件，说 thread 在做什么。
pub(crate) async fn check_threads(
    db: &Database,
    waker: &Waker,
    beats: &Arc<Mutex<CheckBeats<DelegationId>>>,
) -> Result<usize> {
    let (waker, beats) = (waker.clone(), beats.clone());
    db.write(move |tx| {
        let now = Utc::now();
        let reporting: Vec<(Owed, f64)> = owed_in(tx, None, None)?
            .into_iter()
            .filter_map(|o| match o.delegation.kind {
                Delegated::ReportBack { check_seconds } => Some((o, check_seconds)),
                Delegated::Ask => None,
            })
            .collect();
        let mut beats = beats.lock().expect("beats lock");
        beats.keep(&reporting.iter().map(|(o, _)| o.delegation.id).collect::<BTreeSet<_>>());
        // (发起会话, thread) -> 到期的委派与它的间隔：同一个发起方对同一个 thread 的几项委派同时到期，检查一次
        let mut due: BTreeMap<(SessionId, SessionId), (Owed, f64)> = BTreeMap::new();
        for (o, interval) in reporting {
            if !beats.due(&o.delegation.id, o.hold.created_at, interval, now) {
                continue;
            }
            beats.checked(o.delegation.id, now);
            due.entry((o.delegation.parent_id.clone(), o.delegation.thread_id.clone())).or_insert((o, interval));
        }
        drop(beats);
        let child_ids: Vec<SessionId> =
            due.keys().map(|(_, c)| c.clone()).collect::<BTreeSet<_>>().into_iter().collect();
        let states = work_states_in(tx, &child_ids)?;
        let runs = latest_among(tx, &child_ids)?;
        let children = directory::among_in(tx, &child_ids)?;
        let mut posted = 0;
        for ((_, child_id), (o, interval)) in &due {
            // 同上：委派引用的 thread 由外键保证存在
            let child = &children[child_id];
            let agent = agent_in(tx, &child.agent_id)?;
            let text = check_text(child, &agent, states[child_id], runs.get(child_id), now, *interval);
            if post_check_in(tx, &waker, &o.hold, &EventSource::Thread { session_id: child_id.clone() }, &text)? {
                posted += 1;
            }
        }
        Ok(posted)
    })
    .await
}

/// 还没结清的委派的检查信件，按 thread 的工作状态说；run 是它最近一次运行，只用来补充细节。
pub(crate) fn check_text(
    child: &Session,
    agent: &AgentInfo,
    state: WorkState,
    run: Option<&RunRecord>,
    now: DateTime<Utc>,
    check_seconds: f64,
) -> String {
    let name = thread_name(child);
    let who = format!("Thread {name:?} ({})", agent.name);
    let keep = format!(
        "Keep waiting ({}): you will get another check-in in {check_seconds} \
         seconds and an event with its answer; list_threads shows its latest reply. If it is no longer needed, call \
         stop_thread(thread={name:?}, agent={:?}).",
        no_progress_hint(),
        agent.name
    );
    let running = run.filter(|r| r.unfinished());
    let doing = match (state, running, run.and_then(RunRecord::failure)) {
        (WorkState::Waiting, _, _) => "is waiting for work it started (background jobs, other agents), a wake-up it \
             scheduled, or answers it asked for"
            .to_owned(),
        (WorkState::Running, Some(r), _) => {
            let s = (now - r.started_at).num_seconds().max(0);
            return format!("{who} is still working on your task ({}m{:02}s so far).\n{keep}", s / 60, s % 60);
        }
        // 最终失败的收尾自己出错时，结清随收尾一起回滚：委派不会自己结束，如实说，让发起方能自己停
        (WorkState::Idle, _, Some(f)) => {
            return format!(
                "{who} failed on its last run ({}) and will not continue on its own; its failure was not handed back to \
                 you. Call stop_thread(thread={name:?}, agent={:?}) to settle it.",
                truncate(f, 300),
                agent.name
            );
        }
        (WorkState::Running | WorkState::Idle, _, _) => "has your task queued and has not started on it yet".to_owned(),
    };
    format!("{who} {doing}. {keep}")
}
