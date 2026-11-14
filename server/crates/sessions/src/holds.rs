//! 占用（`session_holds` 的唯一读写处）：别的工作替一个会话在外面做的一项异步工作，三种（[`HoldOf`]）：后台任务、到点唤醒、
//! 委派。后台任务的占用记着任务编号（库里唯一），到点唤醒的行（`wakeups`）引用它的占用，委派的占用引用委派行。
//!
//! - 有 active 占用：工作还没做完，交给它的委派不结清。委派的占用还 active 就是这项委派还没结清（唯一的判断）。
//! - 带 call_id 的委派占用：结局作为这次挂起调用（ask_agent）的结果交回；调用在不在等看挂起记录（[`crate::paused_calls`]）。
//! - 结局交回时释放（和一封信件同一事务）；会话被停止、或自己放弃时清掉（之后的结局不再投）。行永久保留。

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, Row, params};

use muliao_platform::db::placeholders;
use muliao_platform::errors::{Result, broken};
use muliao_platform::ids::{DelegationId, HoldId, JobId, SessionId};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HoldStatus {
    Active,
    /// 结局已经交回（和一封信件同一事务）
    Released,
    /// 工作被放弃（会话被停止，或自己改期、取消），之后的结局不再投
    Cleared,
}

impl HoldStatus {
    fn as_str(self) -> &'static str {
        match self {
            HoldStatus::Active => "active",
            HoldStatus::Released => "released",
            HoldStatus::Cleared => "cleared",
        }
    }

    fn parse(s: &str) -> Result<Self> {
        Ok(match s {
            "active" => HoldStatus::Active,
            "released" => HoldStatus::Released,
            "cleared" => HoldStatus::Cleared,
            _ => return Err(broken(format!("unknown hold status {s:?}"))),
        })
    }
}

/// 占用的是哪种工作。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HoldOf {
    /// 后台任务（run_shell background）
    Job { job_id: JobId },
    /// 到点唤醒（wake_me）：约的时刻与说明在引用它的 `wakeups` 行
    Wake,
    /// 委派：row 是委派行（thread 删除后置空）；call_id：结局作为这次挂起调用的结果交回（ask_agent），并发等待的为 None
    Delegation { row: Option<DelegationId>, call_id: Option<String> },
}

impl HoldOf {
    fn kind(&self) -> &'static str {
        match self {
            HoldOf::Job { .. } => "job",
            HoldOf::Wake => "wake",
            HoldOf::Delegation { .. } => "delegation",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Hold {
    pub id: HoldId,
    pub session_id: SessionId,
    pub of: HoldOf,
    /// 用户在等这项工作的结局：交回的信件随它推送
    pub awaited: bool,
    pub status: HoldStatus,
    pub created_at: DateTime<Utc>,
}

impl Hold {
    /// 委派结局要交回的挂起调用（ask_agent）。
    pub(crate) fn call_id(&self) -> Option<&str> {
        match &self.of {
            HoldOf::Delegation { call_id, .. } => call_id.as_deref(),
            HoldOf::Job { .. } | HoldOf::Wake => None,
        }
    }
}

const COLUMNS: &str = "id, session_id, kind, job_id, thread_call_id, call_id, awaited, status, created_at";

type Raw =
    (HoldId, SessionId, String, Option<JobId>, Option<DelegationId>, Option<String>, bool, String, DateTime<Utc>);

fn from_row(r: &Row<'_>) -> rusqlite::Result<Raw> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?))
}

/// 读库时解析一次：种类与它的列对不上是数据坏了。
fn hold((id, session_id, kind, job_id, row, call_id, awaited, status, created_at): Raw) -> Result<Hold> {
    let of = match (kind.as_str(), job_id, call_id) {
        ("job", Some(job_id), None) => HoldOf::Job { job_id },
        ("wake", None, None) => HoldOf::Wake,
        ("delegation", None, call_id) => HoldOf::Delegation { row, call_id },
        _ => return Err(broken(format!("hold {id} of kind {kind:?} does not match its columns"))),
    };
    Ok(Hold { id, session_id, of, awaited, status: HoldStatus::parse(&status)?, created_at })
}

fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Hold>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM session_holds WHERE {filter}"))?;
    let rows = stmt.query_map(params, from_row)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(hold).collect()
}

/// 登记一项后台任务的占用（任务编号每次新生成，不会重复登记）。
pub(crate) fn hold_job_in(c: &Connection, session_id: &SessionId, job_id: JobId, awaited: bool) -> Result<Hold> {
    insert(c, session_id, HoldOf::Job { job_id }, awaited)
}

/// 登记一次到点唤醒的占用；约的时刻与说明由 `wakeups` 记在引用它的行里。
pub(crate) fn hold_wake_in(c: &Connection, session_id: &SessionId, awaited: bool) -> Result<Hold> {
    insert(c, session_id, HoldOf::Wake, awaited)
}

/// 发起方登记一项委派的占用，引用委派行（row）。call_id：结局作为这次挂起调用的结果交回（ask_agent）；并发等待的为 None。
pub(crate) fn hold_delegation_in(
    c: &Connection,
    session_id: &SessionId,
    row: DelegationId,
    call_id: Option<String>,
    awaited: bool,
) -> Result<Hold> {
    insert(c, session_id, HoldOf::Delegation { row: Some(row), call_id }, awaited)
}

fn insert(c: &Connection, session_id: &SessionId, of: HoldOf, awaited: bool) -> Result<Hold> {
    let (job_id, row, call_id) = match &of {
        HoldOf::Job { job_id } => (Some(*job_id), None, None),
        HoldOf::Wake => (None, None, None),
        HoldOf::Delegation { row, call_id } => (None, *row, call_id.as_deref()),
    };
    let now = Utc::now();
    c.execute(
        "INSERT INTO session_holds (session_id, kind, job_id, thread_call_id, call_id, awaited, status, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active', ?7)",
        params![session_id, of.kind(), job_id, row, call_id, awaited, now],
    )?;
    let id = HoldId::new(c.last_insert_rowid());
    Ok(Hold { id, session_id: session_id.clone(), of, awaited, status: HoldStatus::Active, created_at: now })
}

/// 后台任务的占用。
pub(crate) fn job_in(c: &Connection, job_id: JobId) -> Result<Option<Hold>> {
    Ok(query(c, "job_id = ?1", [job_id])?.pop())
}

/// 还 active 的到点唤醒占用，按登记先后；session_id：只看这个会话的。
pub(crate) fn active_wakes_in(c: &Connection, session_id: Option<&SessionId>) -> Result<Vec<Hold>> {
    active_of(c, "wake", session_id)
}

/// 还 active 的委派占用（委派还没结清），按登记先后；session_id：只看这个会话发起的。
pub(crate) fn active_delegations_in(c: &Connection, session_id: Option<&SessionId>) -> Result<Vec<Hold>> {
    active_of(c, "delegation", session_id)
}

fn active_of(c: &Connection, kind: &str, session_id: Option<&SessionId>) -> Result<Vec<Hold>> {
    match session_id {
        Some(s) => query(c, "status = 'active' AND kind = ?1 AND session_id = ?2 ORDER BY id", params![kind, s]),
        None => query(c, "status = 'active' AND kind = ?1 ORDER BY id", [kind]),
    }
}

/// 把还 active 的占用改成 status（按库里的现状，不按调用方手里的副本）。返回改之前的状态。
fn end_active(c: &Connection, hold_id: HoldId, status: HoldStatus) -> Result<HoldStatus> {
    let n = c.execute(
        "UPDATE session_holds SET status = ?1, ended_at = ?2 WHERE id = ?3 AND status = 'active'",
        params![status.as_str(), Utc::now(), hold_id],
    )?;
    if n == 1 {
        return Ok(HoldStatus::Active);
    }
    let now: String = c.query_row("SELECT status FROM session_holds WHERE id = ?1", [hold_id], |r| r.get(0))?;
    HoldStatus::parse(&now)
}

/// 记为已释放，返回释放前的状态。已经被清掉或释放的不变：调用方按返回值决定。
pub(crate) fn release_in(c: &Connection, hold: &Hold) -> Result<HoldStatus> {
    end_active(c, hold.id, HoldStatus::Released)
}

/// 会话自己放弃一项还在进行的工作（改期、取消到点唤醒、后台任务没起来）：记为 cleared，之后的结局不再投。
pub(crate) fn abandon_in(c: &Connection, hold: &Hold) -> Result<()> {
    match end_active(c, hold.id, HoldStatus::Cleared)? {
        HoldStatus::Active => Ok(()),
        _ => Err(broken(format!("hold {} of session {} is not active", hold.id, hold.session_id))),
    }
}

/// 会话被停止：全部 active 占用记为 cleared，返回它们。
pub(crate) fn clear_in(c: &Connection, session_id: &SessionId) -> Result<Vec<Hold>> {
    let holds = query(c, "session_id = ?1 AND status = 'active' ORDER BY id", [session_id])?;
    for h in &holds {
        end_active(c, h.id, HoldStatus::Cleared)?;
    }
    Ok(holds)
}

pub(crate) fn has_active_in(c: &Connection, session_id: &SessionId) -> Result<bool> {
    Ok(c.query_row(
        "SELECT 1 FROM session_holds WHERE session_id = ?1 AND status = 'active' LIMIT 1",
        [session_id],
        |r| r.get::<_, i64>(0),
    )
    .optional()?
    .is_some())
}

/// 其中每个会话的 active 占用，按登记先后（一次查询）；没有的会话不在结果里。
pub(crate) fn active_among(c: &Connection, session_ids: &[SessionId]) -> Result<BTreeMap<SessionId, Vec<Hold>>> {
    let mut held: BTreeMap<SessionId, Vec<Hold>> = BTreeMap::new();
    if session_ids.is_empty() {
        return Ok(held);
    }
    let filter = format!("status = 'active' AND session_id IN ({}) ORDER BY id", placeholders(session_ids.len()));
    for h in query(c, &filter, rusqlite::params_from_iter(session_ids))? {
        held.entry(h.session_id.clone()).or_default().push(h);
    }
    Ok(held)
}
