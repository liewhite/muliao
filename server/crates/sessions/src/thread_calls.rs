//! 调用开出的 thread（`thread_calls` 的唯一读写处）：哪次工具调用开出或续写了哪个 thread，在开出/续写的同一个事务里
//! 记下；界面按调用 id 把 thread 挂在调用条目上，不看调用的结果。
//!
//! 要交回结局的调用（ask_agent、delegate_task(report_back)）就是一项委派，这一行是它的唯一主体：发起方的占用与 thread 的
//! 信件只引用它的 id。委派结清没有只看引用它的占用是否还 active。

use std::collections::BTreeMap;

use rusqlite::{Connection, Row, params};

use crate::error::Result;
use crate::holds::{COLUMN_COUNT, COLUMNS as HOLD_COLUMNS, Hold, from_row, hold};
use crate::mailbox::placeholders;

/// 一次开出或续写 thread 的调用；发起方有引用它的占用时就是一项委派。
#[derive(Debug, Clone, PartialEq)]
pub struct CallRecord {
    pub id: i64,
    /// 发起会话
    pub parent_id: String,
    pub call_id: String,
    /// 做它的 thread
    pub thread_id: String,
    /// report_back 的心跳间隔；ask_agent、普通 delegate_task 为 None
    pub check_seconds: Option<f64>,
}

/// 还没结清的委派与发起方引用它的 active 占用（结局经这个占用交回）。
#[derive(Debug, Clone, PartialEq)]
pub struct Owed {
    pub delegation: CallRecord,
    pub hold: Hold,
}

const COLUMNS: &str = "id, parent_session_id, call_id, thread_session_id, check_seconds";

fn record(r: &Row<'_>) -> rusqlite::Result<CallRecord> {
    Ok(CallRecord {
        id: r.get(0)?,
        parent_id: r.get(1)?,
        call_id: r.get(2)?,
        thread_id: r.get(3)?,
        check_seconds: r.get(4)?,
    })
}

fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<CallRecord>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM thread_calls WHERE {filter}"))?;
    Ok(stmt.query_map(params, record)?.collect::<rusqlite::Result<_>>()?)
}

/// 记下这次调用开出或续写了这个 thread，返回这一行。同一次调用只记一次（调用方先查过 [`call_in`]，表上还有唯一索引）。
pub fn record_call_in(
    c: &Connection,
    parent_id: &str,
    call_id: &str,
    thread_id: &str,
    check_seconds: Option<f64>,
) -> Result<CallRecord> {
    c.execute(
        "INSERT INTO thread_calls (parent_session_id, call_id, thread_session_id, check_seconds) VALUES (?1, ?2, ?3, ?4)",
        params![parent_id, call_id, thread_id, check_seconds],
    )?;
    Ok(call_in(c, parent_id, call_id)?.expect("just recorded"))
}

/// 这次调用记下的那一行（还没记过、或 thread 已删除时为 None）。
pub fn call_in(c: &Connection, parent_id: &str, call_id: &str) -> Result<Option<CallRecord>> {
    Ok(query(c, "parent_session_id = ?1 AND call_id = ?2", params![parent_id, call_id])?.pop())
}

/// 这个会话里每次开出或续写 thread 的调用 → 那个 thread。
pub fn call_threads_in(c: &Connection, parent_id: &str) -> Result<BTreeMap<String, String>> {
    Ok(query(c, "parent_session_id = ?1", [parent_id])?.into_iter().map(|r| (r.call_id, r.thread_id)).collect())
}

/// 这些委派行（thread 已删除的不在结果里）。
pub fn delegations_among(c: &Connection, ids: &[i64]) -> Result<BTreeMap<i64, CallRecord>> {
    if ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let filter = format!("id IN ({})", placeholders(ids.len()));
    Ok(query(c, &filter, rusqlite::params_from_iter(ids))?.into_iter().map(|r| (r.id, r)).collect())
}

/// 还没结清的委派（发起方引用它的占用还 active），按占用登记先后；给了 ids 只看这些委派；session_id：只看这个会话发起的。
pub fn owed_in(c: &Connection, ids: Option<&[i64]>, session_id: Option<&str>) -> Result<Vec<Owed>> {
    let mut filter = vec!["h.status = 'active'".to_owned()];
    let mut values: Vec<&dyn rusqlite::ToSql> = Vec::new();
    match ids {
        None => {}
        Some([]) => return Ok(Vec::new()),
        Some(ids) => {
            filter.push(format!("t.id IN ({})", placeholders(ids.len())));
            values.extend(ids.iter().map(|i| i as &dyn rusqlite::ToSql));
        }
    }
    if let Some(s) = &session_id {
        filter.push(format!("h.session_id = ?{}", values.len() + 1));
        values.push(s);
    }
    let sql = format!(
        "SELECT {HOLD_COLUMNS}, t.id, t.parent_session_id, t.call_id, t.thread_session_id, t.check_seconds \
         FROM session_holds h JOIN thread_calls t ON t.id = h.thread_call_id WHERE {} ORDER BY h.id",
        filter.join(" AND ")
    );
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(values), |r| {
            let delegation = CallRecord {
                id: r.get(COLUMN_COUNT)?,
                parent_id: r.get(COLUMN_COUNT + 1)?,
                call_id: r.get(COLUMN_COUNT + 2)?,
                thread_id: r.get(COLUMN_COUNT + 3)?,
                check_seconds: r.get(COLUMN_COUNT + 4)?,
            };
            Ok((from_row(r)?, delegation))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(|(raw, delegation)| Ok(Owed { hold: hold(raw)?, delegation })).collect()
}
