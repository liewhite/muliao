//! 占用（`session_holds` 的唯一读写处）：别的工作替一个会话在外面做的一项异步工作。外部工作按不透明的占用者认（后台任务
//! `job:<id>`、到点唤醒 `wake:<会话>:<调用>`），委派的占用引用委派行。会话只知道「有人替我在外面做事」，不知道是什么事。
//!
//! - 有 active 占用：工作还没做完，交给它的委派不结清。委派的占用还 active 就是这项委派还没结清（唯一的判断）。
//! - 带 call_id 的占用：这次挂起调用（ask_agent）已经交出去了，不论状态都算，是永久事实。
//! - 结局交回时释放（和一封信件同一事务）；会话被停止、或自己放弃时清掉（之后的结局不再投）。行永久保留。

use std::collections::{BTreeMap, HashSet};

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, Row, params};

use crate::error::{Result, broken};
use crate::mailbox::placeholders;

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

#[derive(Debug, Clone, PartialEq)]
pub struct Hold {
    pub id: i64,
    pub session_id: String,
    pub holder: Option<String>,
    pub thread_call_id: Option<i64>,
    pub call_id: Option<String>,
    /// 用户在等这项工作的结局：交回的信件随它推送
    pub awaited: bool,
    pub status: HoldStatus,
    pub created_at: DateTime<Utc>,
}

const COLUMNS: &str = "id, session_id, holder, thread_call_id, call_id, awaited, status, created_at";

type Raw = (i64, String, Option<String>, Option<i64>, Option<String>, bool, String, DateTime<Utc>);

fn raw(r: &Row<'_>) -> rusqlite::Result<Raw> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?))
}

fn hold((id, session_id, holder, thread_call_id, call_id, awaited, status, created_at): Raw) -> Result<Hold> {
    Ok(Hold {
        id,
        session_id,
        holder,
        thread_call_id,
        call_id,
        awaited,
        status: HoldStatus::parse(&status)?,
        created_at,
    })
}

fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Hold>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM session_holds WHERE {filter}"))?;
    let rows = stmt.query_map(params, raw)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(hold).collect()
}

fn insert(
    c: &Connection,
    session_id: &str,
    holder: Option<&str>,
    thread_call_id: Option<i64>,
    call_id: Option<&str>,
    awaited: bool,
) -> Result<Hold> {
    c.execute(
        "INSERT INTO session_holds (session_id, holder, thread_call_id, call_id, awaited, status, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, 'active', ?6)",
        params![session_id, holder, thread_call_id, call_id, awaited, Utc::now()],
    )?;
    let id = c.last_insert_rowid();
    Ok(query(c, "id = ?1", [id])?.pop().expect("just inserted"))
}

/// 登记一项外部工作的占用；已经登记过（重放）就返回它。
pub fn hold_in(c: &Connection, session_id: &str, holder: &str, awaited: bool) -> Result<Hold> {
    if let Some(existing) = get_in(c, session_id, holder)? {
        assert_eq!(
            existing.awaited, awaited,
            "hold {holder} of session {session_id} was registered with another awaited"
        );
        return Ok(existing);
    }
    insert(c, session_id, Some(holder), None, None, awaited)
}

/// 发起方登记一项委派的占用，引用委派行。call_id：结局作为这次挂起调用的结果交回（ask_agent）；并发等待的为 None。
pub fn hold_delegation_in(
    c: &Connection,
    session_id: &str,
    thread_call_id: i64,
    call_id: Option<&str>,
    awaited: bool,
) -> Result<Hold> {
    insert(c, session_id, None, Some(thread_call_id), call_id, awaited)
}

/// 外部工作在会话上的占用。
pub fn get_in(c: &Connection, session_id: &str, holder: &str) -> Result<Option<Hold>> {
    Ok(query(c, "session_id = ?1 AND holder = ?2", params![session_id, holder])?.pop())
}

/// 引用这些委派行、还 active 的占用（还没结清的委派），按登记先后；None 是全部。
pub fn active_delegations_in(c: &Connection, thread_call_ids: Option<&[i64]>) -> Result<Vec<Hold>> {
    match thread_call_ids {
        None => query(c, "status = 'active' AND thread_call_id IS NOT NULL ORDER BY id", []),
        Some([]) => Ok(Vec::new()),
        Some(ids) => query(
            c,
            &format!("status = 'active' AND thread_call_id IN ({}) ORDER BY id", placeholders(ids.len())),
            rusqlite::params_from_iter(ids),
        ),
    }
}

fn set_status(c: &Connection, hold_id: i64, status: HoldStatus) -> Result<()> {
    c.execute(
        "UPDATE session_holds SET status = ?1, ended_at = ?2 WHERE id = ?3",
        params![status.as_str(), Utc::now(), hold_id],
    )?;
    Ok(())
}

/// 记为已释放，返回释放前的状态。已经被清掉的不变：调用方不再投信。
pub fn release_in(c: &Connection, hold: &Hold) -> Result<HoldStatus> {
    if hold.status == HoldStatus::Active {
        set_status(c, hold.id, HoldStatus::Released)?;
    }
    Ok(hold.status)
}

/// 会话自己放弃一项还在进行的工作（改期、取消到点唤醒、后台任务没起来）：记为 cleared，之后的结局不再投。
pub fn abandon_in(c: &Connection, hold: &Hold) -> Result<()> {
    if hold.status != HoldStatus::Active {
        return Err(broken(format!("hold {} of session {} is not active", hold.id, hold.session_id)));
    }
    set_status(c, hold.id, HoldStatus::Cleared)
}

/// 会话被停止：全部 active 占用记为 cleared，返回它们。
pub fn clear_in(c: &Connection, session_id: &str) -> Result<Vec<Hold>> {
    let holds = query(c, "session_id = ?1 AND status = 'active' ORDER BY id", [session_id])?;
    for h in &holds {
        set_status(c, h.id, HoldStatus::Cleared)?;
    }
    Ok(holds)
}

/// 这个会话的外部工作的 active 占用者。
pub fn active_holders_of_in(c: &Connection, session_id: &str) -> Result<Vec<String>> {
    Ok(query(c, "session_id = ?1 AND status = 'active' AND holder IS NOT NULL ORDER BY id", [session_id])?
        .into_iter()
        .filter_map(|h| h.holder)
        .collect())
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
