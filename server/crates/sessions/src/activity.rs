//! 会话忙不忙：别的领域与删除规则问的两个不同问题（docs/architecture.md 第 7 节）。
//!
//! - 工作状态（[`work_states_in`]：running / waiting / idle，唯一规则）：会话列表、issue 看板、触发器防重叠、list_threads。
//!   running = 有没结束的运行，或信箱里有按唤醒规则（[`crate::execution::actor::should_run`]）要处理的信；waiting = 运行停了但在等
//!   外面的结果（active 占用，或还能回答的提问、还能批准的调用）；否则 idle。
//! - 删除会不会打断工作（[`blocking_deletion_in`]）：挂起的提问没有人在外面做事，不挡删除。
//! - 等待项（[`waits_of_in`]）：waiting 的原因逐项展开，会话页列出。

use muliao_platform::ids::{AgentId, CallId, HoldId, JobId, SessionId};
use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Utc};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};

use crate::execution::actor::should_run;
use crate::execution::runs::{latest_among, unfinished_among};
use crate::intake::directory::{self, keyed_among};
use crate::intake::origins::Sender;
use crate::mailbox::queued_among;
use crate::outside::holds::{Hold, HoldOf, active_among};
use crate::outside::wakeups::pending_of_in;
use crate::record::history::since_last_response_among;
pub use crate::thread_calls::Delegated;
use crate::thread_calls::owed_in;
use crate::waiting::answers::Question;
use crate::waiting::paused_calls::kinds_among;
use crate::waiting::pending::{OpenAsk, OpenAsks, open_asks_of, position};
use muliao_platform::errors::Result;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum WorkState {
    /// 有没结束的运行，或有要处理的信
    Running,
    /// 运行停了，但在等外面的结果：后台任务、委派、到点唤醒，或提问、等批准的调用等用户
    Waiting,
    Idle,
}

muliao_platform::serde_names!(WorkState);

/// 一个会话为什么在 waiting（唯一定义）：active 占用，与还能回答的提问、还能批准的调用。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Waiting {
    /// 按登记先后
    pub holds: Vec<Hold>,
    pub asks: OpenAsks,
}

impl Waiting {
    pub(crate) fn any(&self) -> bool {
        !self.holds.is_empty() || self.asks.any_open()
    }
}

/// 一批会话的状态，查询次数与会话数无关。
pub struct Status {
    pub state: WorkState,
    pub waiting: Waiting,
}

pub(crate) fn statuses_in(c: &Connection, session_ids: &[SessionId]) -> Result<BTreeMap<SessionId, Status>> {
    let unfinished: BTreeSet<SessionId> = unfinished_among(c, session_ids)?.into_iter().collect();
    let mut queued = queued_among(c, session_ids)?;
    let mut tails = since_last_response_among(c, session_ids)?;
    let mut paused = kinds_among(c, session_ids)?;
    let latest = latest_among(c, session_ids)?;
    let mut held = active_among(c, session_ids)?;
    let mut out = BTreeMap::new();
    for id in session_ids {
        let queued = queued.remove(id).unwrap_or_default();
        let at = position(&tails.remove(id).unwrap_or_default(), &paused.remove(id).unwrap_or_default(), &queued)?;
        let waiting = Waiting { holds: held.remove(id).unwrap_or_default(), asks: open_asks_of(&at, &queued) };
        let state = if unfinished.contains(id) || should_run(&queued, latest.get(id), &at) {
            WorkState::Running
        } else if waiting.any() {
            WorkState::Waiting
        } else {
            WorkState::Idle
        };
        out.insert(id.clone(), Status { state, waiting });
    }
    Ok(out)
}

/// 一批会话的工作状态。
pub(crate) fn work_states_in(c: &Connection, session_ids: &[SessionId]) -> Result<BTreeMap<SessionId, WorkState>> {
    Ok(statuses_in(c, session_ids)?.into_iter().map(|(k, s)| (k, s.state)).collect())
}

pub fn work_state_in(c: &Connection, session_id: &SessionId) -> Result<WorkState> {
    Ok(work_states_in(c, std::slice::from_ref(session_id))?[session_id])
}

/// 一批（可能已删除的）会话里还在的那些的工作状态，按 id；已删除的不在结果里。
pub fn states_of_in(c: &Connection, session_ids: &[SessionId]) -> Result<BTreeMap<SessionId, WorkState>> {
    let existing: Vec<SessionId> = directory::among_in(c, session_ids)?.into_keys().collect();
    work_states_in(c, &existing)
}

/// 按发送方定位的会话的工作状态（key：发送方与 agent id）；还没有这样的会话时为 None。
pub fn keyed_states_in(
    c: &Connection,
    keys: &[(Sender, AgentId)],
) -> Result<BTreeMap<(Sender, AgentId), Option<WorkState>>> {
    let senders: Vec<(Sender, Sender)> =
        keys.iter().map(|(s, _)| *s).collect::<BTreeSet<_>>().into_iter().map(|s| (s, s)).collect();
    let found = keyed_among(c, &senders)?;
    let ids: Vec<SessionId> = found.values().map(|s| s.id.clone()).collect();
    let states = work_states_in(c, &ids)?;
    Ok(keys.iter().map(|k| (k.clone(), found.get(k).map(|s| states[&s.id]))).collect())
}

/// 其中忙、因而不能删除的会话：有没结束的运行或要处理的信，或有人替它在外面做事。挂起的提问不挡删除。
pub(crate) fn blocking_deletion_in(c: &Connection, session_ids: &[SessionId]) -> Result<Vec<SessionId>> {
    Ok(statuses_in(c, session_ids)?
        .into_iter()
        .filter(|(_, s)| s.state == WorkState::Running || !s.waiting.holds.is_empty())
        .map(|(id, _)| id)
        .collect())
}

/// 归档前的检查：它的会话里有忙的（与删除 agent 时的判断同一条规则）。
pub(crate) fn blocks_agent_in(c: &Connection, agent_id: &AgentId) -> Result<bool> {
    let ids: Vec<SessionId> = directory::of_agent_in(c, agent_id)?.into_iter().map(|s| s.id).collect();
    Ok(!blocking_deletion_in(c, &ids)?.is_empty())
}

/// 会话在等的一项东西（会话页列出）。
#[derive(Debug, Clone, PartialEq)]
pub enum Wait {
    /// 后台任务（run_shell background）
    Job { job_id: JobId, since: DateTime<Utc> },
    /// 委派出去的工作（ask_agent，或 delegate_task 带 report_back）
    Delegation { thread_session_id: SessionId, mode: Delegated, since: DateTime<Utc> },
    /// 自己约的到点唤醒
    Wake { due_at: DateTime<Utc>, note: String, since: DateTime<Utc> },
    /// 用户回答提问
    Ask { call_id: CallId, headers: Vec<String> },
    /// 用户批准一次工具调用
    Approval { call_id: CallId, tool_name: String },
}

/// 会话在等的东西逐项展开：占用按登记先后，然后是提问、等批准的调用。
pub(crate) fn waits_of_in(c: &Connection, session_id: &SessionId, waiting: &Waiting) -> Result<Vec<Wait>> {
    // (占用 id, 等待项)：委派与唤醒的详情各自和占用联查，后台任务的占用本身就够
    let mut held: Vec<(HoldId, Wait)> = waiting
        .holds
        .iter()
        .filter_map(|h| match h.of {
            HoldOf::Job { job_id } => Some((h.id, Wait::Job { job_id, since: h.created_at })),
            HoldOf::Wake | HoldOf::Delegation { .. } | HoldOf::DeletedThread => None,
        })
        .collect();
    held.extend(owed_in(c, None, Some(session_id))?.into_iter().map(|o| {
        let wait = Wait::Delegation {
            thread_session_id: o.delegation.thread_id,
            mode: o.delegation.kind,
            since: o.hold.created_at,
        };
        (o.hold.id, wait)
    }));
    held.extend(
        pending_of_in(c, session_id)?
            .into_iter()
            .map(|(h, due_at, note)| (h.id, Wait::Wake { due_at, note, since: h.created_at })),
    );
    held.sort_by_key(|(id, _)| *id);
    let mut waits: Vec<Wait> = held.into_iter().map(|(_, w)| w).collect();
    for (call_id, ask) in &waiting.asks.0 {
        if let OpenAsk::Question(questions) = ask {
            waits.push(Wait::Ask {
                call_id: call_id.clone(),
                headers: questions.iter().map(|q: &Question| q.header.clone()).collect(),
            });
        }
    }
    for (call_id, ask) in &waiting.asks.0 {
        if let OpenAsk::Approval(tool) = ask {
            waits.push(Wait::Approval { call_id: call_id.clone(), tool_name: tool.clone() });
        }
    }
    Ok(waits)
}
