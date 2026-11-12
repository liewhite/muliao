//! 会话忙不忙：别的领域与删除规则问的两个不同问题（docs/architecture.md 第 7 节）。
//!
//! - 工作状态（[`work_states_in`]：running / waiting / idle，唯一规则）：会话列表、issue 看板、触发器防重叠、list_threads。
//!   running = 有没结束的运行，或信箱里有按唤醒规则（[`crate::actor::should_run`]）要处理的信；waiting = 运行停了但在等
//!   外面的结果（active 占用，或还能回答的提问、还能批准的调用）；否则 idle。
//! - 删除会不会打断工作（[`blocking_deletion_in`]）：挂起的提问没有人在外面做事，不挡删除。
//! - 等待项（[`waits_of_in`]）：waiting 的原因逐项展开，会话页列出。

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Utc};
use rusqlite::Connection;
use serde::Serialize;

use crate::actor::should_run;
use crate::answers::Question;
use crate::approvals::requested_among;
use crate::directory::{self, keyed_among};
use crate::error::{Result, broken};
use crate::history::since_last_response_among;
use crate::holds::{Hold, active_among, called_among};
use crate::mailbox::{placeholders, queued_among};
use crate::origins::Sender;
use crate::pending::{HandedOut, OpenAsks, open_asks_of, position};
use crate::runs::{latest_among, unfinished_among};
use crate::thread_calls::delegations_among;
use crate::work::job_id_of_holder;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum WorkState {
    /// 有没结束的运行，或有要处理的信
    Running,
    /// 运行停了，但在等外面的结果：后台任务、委派、到点唤醒，或提问、等批准的调用等用户
    Waiting,
    Idle,
}

impl WorkState {
    pub fn as_str(self) -> &'static str {
        match self {
            WorkState::Running => "running",
            WorkState::Waiting => "waiting",
            WorkState::Idle => "idle",
        }
    }
}

/// 一个会话为什么在 waiting（唯一定义）：active 占用，与还能回答的提问、还能批准的调用。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Waiting {
    /// 按登记先后
    pub holds: Vec<Hold>,
    pub asks: OpenAsks,
}

impl Waiting {
    pub fn any(&self) -> bool {
        !self.holds.is_empty() || !self.asks.is_empty()
    }
}

/// 一批会话的状态，查询次数与会话数无关。
pub struct Status {
    pub state: WorkState,
    pub waiting: Waiting,
}

pub fn statuses_in(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, Status>> {
    let unfinished: BTreeSet<String> = unfinished_among(c, session_ids)?.into_iter().collect();
    let mut queued = queued_among(c, session_ids)?;
    let mut tails = since_last_response_among(c, session_ids)?;
    let mut called = called_among(c, session_ids)?;
    let mut requested = requested_among(c, session_ids)?;
    let latest = latest_among(c, session_ids)?;
    let mut held = active_among(c, session_ids)?;
    let mut out = BTreeMap::new();
    for id in session_ids {
        let queued = queued.remove(id).unwrap_or_default();
        let handed = HandedOut {
            delegated: called.remove(id).unwrap_or_default(),
            approvals: requested.remove(id).unwrap_or_default(),
        };
        let at = position(&tails.remove(id).unwrap_or_default(), &handed, &queued)?;
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
pub fn work_states_in(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, WorkState>> {
    Ok(statuses_in(c, session_ids)?.into_iter().map(|(k, s)| (k, s.state)).collect())
}

pub fn work_state_in(c: &Connection, session_id: &str) -> Result<WorkState> {
    Ok(work_states_in(c, &[session_id.to_owned()])?[session_id])
}

/// 一批（可能已删除的）会话的工作状态，按顺序；已删除的是 None。
pub fn states_of_in(c: &Connection, session_ids: &[String]) -> Result<Vec<Option<WorkState>>> {
    let existing: Vec<String> = directory::among_in(c, session_ids)?.into_keys().collect();
    let states = work_states_in(c, &existing)?;
    Ok(session_ids.iter().map(|id| states.get(id).copied()).collect())
}

/// 按发送方定位的会话的工作状态（key：发送方与 agent id）；还没有这样的会话时为 None。
pub fn keyed_states_in(
    c: &Connection,
    keys: &[(Sender, String)],
) -> Result<BTreeMap<(Sender, String), Option<WorkState>>> {
    let senders: Vec<Sender> = keys.iter().map(|(s, _)| *s).collect::<BTreeSet<_>>().into_iter().collect();
    let found = keyed_among(c, &senders, None)?;
    let ids: Vec<String> = found.values().map(|s| s.id.clone()).collect();
    let states = work_states_in(c, &ids)?;
    Ok(keys.iter().map(|k| (k.clone(), found.get(k).map(|s| states[&s.id]))).collect())
}

/// 其中忙、因而不能删除的会话：有没结束的运行或要处理的信，或有人替它在外面做事。挂起的提问不挡删除。
pub fn blocking_deletion_in(c: &Connection, session_ids: &[String]) -> Result<Vec<String>> {
    Ok(statuses_in(c, session_ids)?
        .into_iter()
        .filter(|(_, s)| s.state == WorkState::Running || !s.waiting.holds.is_empty())
        .map(|(id, _)| id)
        .collect())
}

/// 归档前的检查：它的会话里有忙的（与删除 agent 时的判断同一条规则）。
pub fn blocks_agent_in(c: &Connection, agent_id: &str) -> Result<bool> {
    let ids: Vec<String> = directory::of_agent_in(c, agent_id)?.into_iter().map(|s| s.id).collect();
    Ok(!blocking_deletion_in(c, &ids)?.is_empty())
}

/// 会话在等的一项东西（会话页列出）。
#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Wait {
    /// 后台任务（run_shell background）
    Job { job_id: i64, since: DateTime<Utc> },
    /// 委派出去的工作（ask_agent，或 delegate_task 带 report_back）
    Delegation { thread_session_id: String, check_seconds: Option<f64>, since: DateTime<Utc> },
    /// 自己约的到点唤醒
    Wake { due_at: DateTime<Utc>, note: String, since: DateTime<Utc> },
    /// 用户回答提问
    Ask { call_id: String, headers: Vec<String> },
    /// 用户批准一次工具调用
    Approval { call_id: String, tool_name: String },
}

/// 会话在等的东西逐项展开：占用按登记先后，然后是提问、等批准的调用。认不出的占用者是缺陷，直接报错。
pub fn waits_of_in(c: &Connection, waiting: &Waiting) -> Result<Vec<Wait>> {
    let delegations = delegations_among(c, &waiting.holds.iter().filter_map(|h| h.thread_call_id).collect::<Vec<_>>())?;
    let holders: Vec<String> = waiting.holds.iter().filter_map(|h| h.holder.clone()).collect();
    let wakeups = wakeups_among(c, &holders)?;
    let mut waits = Vec::new();
    for h in &waiting.holds {
        let wait = if let Some(job_id) = h.holder.as_deref().and_then(job_id_of_holder) {
            Wait::Job { job_id, since: h.created_at }
        } else if let Some(d) = h.thread_call_id.and_then(|id| delegations.get(&id)) {
            Wait::Delegation {
                thread_session_id: d.thread_id.clone(),
                check_seconds: d.check_seconds,
                since: h.created_at,
            }
        } else if let Some((due_at, note)) = h.holder.as_ref().and_then(|x| wakeups.get(x)) {
            Wait::Wake { due_at: *due_at, note: note.clone(), since: h.created_at }
        } else {
            return Err(broken(format!(
                "active hold {} ({:?}) of session {} is of no known kind",
                h.id, h.holder, h.session_id
            )));
        };
        waits.push(wait);
    }
    for (call_id, questions) in &waiting.asks.answerable {
        waits.push(Wait::Ask {
            call_id: call_id.clone(),
            headers: questions.iter().map(|q: &Question| q.header.clone()).collect(),
        });
    }
    for (call_id, tool) in &waiting.asks.approvable {
        waits.push(Wait::Approval { call_id: call_id.clone(), tool_name: tool.clone() });
    }
    Ok(waits)
}

/// 这些占用者里是到点唤醒的那些约的时刻与说明。
fn wakeups_among(c: &Connection, holders: &[String]) -> Result<BTreeMap<String, (DateTime<Utc>, String)>> {
    if holders.is_empty() {
        return Ok(BTreeMap::new());
    }
    let sql = format!("SELECT holder, due_at, note FROM wakeups WHERE holder IN ({})", placeholders(holders.len()));
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(holders), |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?))))?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}
