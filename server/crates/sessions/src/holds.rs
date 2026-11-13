//! 占用（`session_holds` 的唯一读写处）：别的工作替一个会话在外面做的一项异步工作，三种（[`HoldOf`]）：后台任务、到点唤醒、
//! 委派。后台任务与到点唤醒按占用者（`job:<id>`、`wake:<会话>:<调用>`，库里的唯一键）认，委派的占用引用委派行。
//!
//! - 有 active 占用：工作还没做完，交给它的委派不结清。委派的占用还 active 就是这项委派还没结清（唯一的判断）。
//! - 带 call_id 的委派占用：这次挂起调用（ask_agent）已经交出去了，不论状态都算，是永久事实。
//! - 结局交回时释放（和一封信件同一事务）；会话被停止、或自己放弃时清掉（之后的结局不再投）。行永久保留。

use std::collections::{BTreeMap, HashSet};

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, Row, params};

use crate::mailbox::placeholders;
use muliao_platform::errors::{Result, broken};

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
    Job { job_id: i64 },
    /// 到点唤醒：约它的那次 wake_me 调用
    Wake { call_id: String },
    /// 委派：row 是委派行（thread 删除后置空）；call_id：结局作为这次挂起调用的结果交回（ask_agent），并发等待的为 None
    Delegation { row: Option<i64>, call_id: Option<String> },
}

impl HoldOf {
    fn kind(&self) -> &'static str {
        match self {
            HoldOf::Job { .. } => "job",
            HoldOf::Wake { .. } => "wake",
            HoldOf::Delegation { .. } => "delegation",
        }
    }
}

/// 按占用者认的两种（后台任务、到点唤醒）：登记与查找用；委派的占用引用委派行，另由 [`hold_delegation_in`] 登记。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outside {
    Job { job_id: i64 },
    Wake { call_id: String },
}

impl Outside {
    /// 库里的占用者（唯一键）。
    pub(crate) fn holder(&self, session_id: &str) -> String {
        match self {
            Outside::Job { job_id } => format!("job:{job_id}"),
            Outside::Wake { call_id } => format!("wake:{session_id}:{call_id}"),
        }
    }
}

impl From<Outside> for HoldOf {
    fn from(o: Outside) -> Self {
        match o {
            Outside::Job { job_id } => HoldOf::Job { job_id },
            Outside::Wake { call_id } => HoldOf::Wake { call_id },
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Hold {
    pub id: i64,
    pub session_id: String,
    pub of: HoldOf,
    /// 用户在等这项工作的结局：交回的信件随它推送
    pub awaited: bool,
    pub status: HoldStatus,
    pub created_at: DateTime<Utc>,
}

impl Hold {
    /// 委派结局要交回的挂起调用（ask_agent）。
    pub fn call_id(&self) -> Option<&str> {
        match &self.of {
            HoldOf::Delegation { call_id, .. } => call_id.as_deref(),
            _ => None,
        }
    }
}

/// 别的表与占用联查时取的列（表别名 h），与 [`from_row`] 对应。
pub(crate) const COLUMNS: &str =
    "h.id, h.session_id, h.kind, h.holder, h.thread_call_id, h.call_id, h.awaited, h.status, h.created_at";
/// [`COLUMNS`] 的列数：联查时其后的列从这里数起。
pub(crate) const COLUMN_COUNT: usize = 9;

type Raw = (i64, String, String, Option<String>, Option<i64>, Option<String>, bool, String, DateTime<Utc>);

pub(crate) fn from_row(r: &Row<'_>) -> rusqlite::Result<Raw> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?))
}

/// 读库时解析一次：种类与它的列对不上是数据坏了。
pub(crate) fn hold((id, session_id, kind, holder, row, call_id, awaited, status, created_at): Raw) -> Result<Hold> {
    let of = match (kind.as_str(), holder, call_id) {
        ("job", Some(holder), None) => {
            holder.strip_prefix("job:").and_then(|n| n.parse().ok()).map(|job_id| HoldOf::Job { job_id })
        }
        ("wake", Some(holder), None) => holder
            .strip_prefix(&format!("wake:{session_id}:"))
            .map(|call_id| HoldOf::Wake { call_id: call_id.to_owned() }),
        ("delegation", None, call_id) => Some(HoldOf::Delegation { row, call_id }),
        _ => None,
    };
    let of = of.ok_or_else(|| broken(format!("hold {id} of kind {kind:?} does not match its columns")))?;
    Ok(Hold { id, session_id, of, awaited, status: HoldStatus::parse(&status)?, created_at })
}

fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Hold>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM session_holds h WHERE {filter}"))?;
    let rows = stmt.query_map(params, from_row)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(hold).collect()
}

/// 登记一项后台任务或到点唤醒的占用（各自的 id 每次新生成，不会重复登记）。
pub fn hold_in(c: &Connection, session_id: &str, outside: Outside, awaited: bool) -> Result<Hold> {
    let holder = outside.holder(session_id);
    insert(c, session_id, Some(&holder), outside.into(), awaited)
}

/// 发起方登记一项委派的占用，引用委派行（row）。call_id：结局作为这次挂起调用的结果交回（ask_agent）；并发等待的为 None。
pub fn hold_delegation_in(
    c: &Connection,
    session_id: &str,
    row: i64,
    call_id: Option<String>,
    awaited: bool,
) -> Result<Hold> {
    insert(c, session_id, None, HoldOf::Delegation { row: Some(row), call_id }, awaited)
}

/// holder：后台任务与到点唤醒的占用者（委派没有）；委派行与挂起调用从 of 展开。
fn insert(c: &Connection, session_id: &str, holder: Option<&str>, of: HoldOf, awaited: bool) -> Result<Hold> {
    let (row, call_id) = match &of {
        HoldOf::Delegation { row, call_id } => (*row, call_id.as_deref()),
        HoldOf::Job { .. } | HoldOf::Wake { .. } => (None, None),
    };
    let now = Utc::now();
    c.execute(
        "INSERT INTO session_holds (session_id, kind, holder, thread_call_id, call_id, awaited, status, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active', ?7)",
        params![session_id, of.kind(), holder, row, call_id, awaited, now],
    )?;
    let id = c.last_insert_rowid();
    Ok(Hold { id, session_id: session_id.to_owned(), of, awaited, status: HoldStatus::Active, created_at: now })
}

/// 后台任务或到点唤醒在会话上的占用。
pub fn get_in(c: &Connection, session_id: &str, outside: &Outside) -> Result<Option<Hold>> {
    let holder = outside.holder(session_id);
    Ok(query(c, "h.session_id = ?1 AND h.holder = ?2", params![session_id, holder])?.pop())
}

/// 把还 active 的占用改成 status（按库里的现状，不按调用方手里的副本）。返回改之前的状态。
fn end_active(c: &Connection, hold_id: i64, status: HoldStatus) -> Result<HoldStatus> {
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
pub fn release_in(c: &Connection, hold: &Hold) -> Result<HoldStatus> {
    end_active(c, hold.id, HoldStatus::Released)
}

/// 会话自己放弃一项还在进行的工作（改期、取消到点唤醒、后台任务没起来）：记为 cleared，之后的结局不再投。
pub fn abandon_in(c: &Connection, hold: &Hold) -> Result<()> {
    match end_active(c, hold.id, HoldStatus::Cleared)? {
        HoldStatus::Active => Ok(()),
        _ => Err(broken(format!("hold {} of session {} is not active", hold.id, hold.session_id))),
    }
}

/// 会话被停止：全部 active 占用记为 cleared，返回它们。
pub fn clear_in(c: &Connection, session_id: &str) -> Result<Vec<Hold>> {
    let holds = query(c, "session_id = ?1 AND status = 'active' ORDER BY id", [session_id])?;
    for h in &holds {
        end_active(c, h.id, HoldStatus::Cleared)?;
    }
    Ok(holds)
}

pub fn has_active_in(c: &Connection, session_id: &str) -> Result<bool> {
    Ok(c.query_row(
        "SELECT 1 FROM session_holds WHERE session_id = ?1 AND status = 'active' LIMIT 1",
        [session_id],
        |r| r.get::<_, i64>(0),
    )
    .optional()?
    .is_some())
}

/// 其中每个会话的 active 占用，按登记先后（一次查询）；没有的会话不在结果里。
pub fn active_among(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, Vec<Hold>>> {
    let mut held: BTreeMap<String, Vec<Hold>> = BTreeMap::new();
    if session_ids.is_empty() {
        return Ok(held);
    }
    let filter = format!("status = 'active' AND session_id IN ({}) ORDER BY id", placeholders(session_ids.len()));
    for h in query(c, &filter, rusqlite::params_from_iter(session_ids))? {
        held.entry(h.session_id.clone()).or_default().push(h);
    }
    Ok(held)
}

/// 这些会话里已经交出去的挂起调用 id（带 call_id 的占用，不论状态），每个会话都在结果里。
pub fn called_among(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, HashSet<String>>> {
    let mut found: BTreeMap<String, HashSet<String>> =
        session_ids.iter().map(|s| (s.clone(), HashSet::new())).collect();
    if session_ids.is_empty() {
        return Ok(found);
    }
    let sql = format!(
        "SELECT session_id, call_id FROM session_holds WHERE call_id IS NOT NULL AND session_id IN ({})",
        placeholders(session_ids.len())
    );
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(session_ids), |r| Ok((r.get::<_, String>(0)?, r.get(1)?)))?;
    for row in rows {
        let (s, call) = row?;
        found.entry(s).or_default().insert(call);
    }
    Ok(found)
}
