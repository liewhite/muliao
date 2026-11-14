//! 调用开出的 thread（`thread_calls` 的唯一读写处）：哪次工具调用用哪种方式开出或续写了哪个 thread，在开出/续写的
//! 同一个事务里记下；界面按调用 id 把 thread 挂在调用条目上，不看调用的结果。
//!
//! 方式（[`CallMode`]）只存在这一行（#847），不从占用、挂起记录或间隔反推。要交回结局的调用（ask_agent、
//! delegate_task(report_back)）就是一项委派，这一行是它的唯一主体：发起方的占用与 thread 的信件只引用它的 id。
//! 委派结清没有只看引用它的占用是否还 active。

use muliao_platform::ids::{DelegationId, SessionId};
use std::collections::BTreeMap;

use rusqlite::{Connection, Row, params};

use crate::holds::{Hold, HoldOf, active_delegations_in};
use muliao_platform::db::placeholders;
use muliao_platform::errors::{Result, broken};

/// 一次往 thread 投信的调用用的哪种方式。
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum CallMode {
    /// 普通 delegate_task：不交回结局
    Plain,
    /// 要交回结局：这一行就是一项委派
    Delegated(Delegated),
}

/// 委派是哪一种。
#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Delegated {
    /// delegate_task(report_back)：结局作为事件交回，每隔 check_seconds 秒给发起方投一封运行中检查
    ReportBack { check_seconds: f64 },
    /// ask_agent：发起方停在这次调用上，没有检查也没有超时
    Ask,
}

impl CallMode {
    /// 存下的形状：(mode 列, check_seconds 列)，表上的 CHECK 约束只允许这几种组合。
    fn columns(self) -> (&'static str, Option<f64>) {
        match self {
            CallMode::Plain => ("plain", None),
            CallMode::Delegated(Delegated::ReportBack { check_seconds }) => ("report_back", Some(check_seconds)),
            CallMode::Delegated(Delegated::Ask) => ("ask", None),
        }
    }

    fn parse(mode: &str, check_seconds: Option<f64>) -> Option<CallMode> {
        match (mode, check_seconds) {
            ("plain", None) => Some(CallMode::Plain),
            ("report_back", Some(check_seconds)) => Some(CallMode::Delegated(Delegated::ReportBack { check_seconds })),
            ("ask", None) => Some(CallMode::Delegated(Delegated::Ask)),
            _ => None,
        }
    }
}

/// 一次开出或续写 thread 的调用。
#[derive(Debug, Clone, PartialEq)]
pub struct CallRecord {
    pub id: DelegationId,
    /// 发起会话
    pub parent_id: SessionId,
    pub call_id: String,
    /// 做它的 thread
    pub thread_id: SessionId,
    pub mode: CallMode,
}

/// 一项委派：方式是 [`CallMode::Delegated`] 的调用。
#[derive(Debug, Clone, PartialEq)]
pub struct Delegation {
    pub id: DelegationId,
    pub parent_id: SessionId,
    pub call_id: String,
    pub thread_id: SessionId,
    pub kind: Delegated,
}

/// 还没结清的委派与发起方引用它的 active 占用（结局经这个占用交回）。
#[derive(Debug, Clone, PartialEq)]
pub struct Owed {
    pub delegation: Delegation,
    pub hold: Hold,
}

const COLUMNS: &str = "id, parent_session_id, call_id, thread_session_id, mode, check_seconds";

type RawRecord = (DelegationId, SessionId, String, SessionId, String, Option<f64>);

fn raw(r: &Row<'_>) -> rusqlite::Result<RawRecord> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?))
}

fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<CallRecord>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM thread_calls WHERE {filter}"))?;
    let rows = stmt.query_map(params, raw)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter()
        .map(|(id, parent_id, call_id, thread_id, mode, check_seconds)| {
            // 列上的 CHECK 约束只允许 parse 认得的组合
            let mode = CallMode::parse(&mode, check_seconds).ok_or_else(|| {
                broken(format!("thread call {id} has mode {mode} with check_seconds {check_seconds:?}"))
            })?;
            Ok(CallRecord { id, parent_id, call_id, thread_id, mode })
        })
        .collect()
}

/// 记下这次调用用这种方式开出或续写了这个 thread，返回这一行。同一次调用只执行一次（被打断的调用不重做），
/// 表上的唯一索引挡住重复。
pub(crate) fn record_call_in(
    c: &Connection,
    parent_id: &SessionId,
    call_id: &str,
    thread_id: &SessionId,
    mode: CallMode,
) -> Result<CallRecord> {
    let (mode_column, check_seconds) = mode.columns();
    c.execute(
        "INSERT INTO thread_calls (parent_session_id, call_id, thread_session_id, mode, check_seconds) \
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![parent_id, call_id, thread_id, mode_column, check_seconds],
    )?;
    Ok(call_in(c, parent_id, call_id)?.expect("just recorded"))
}

/// 这次调用记下的那一行（还没记过、或 thread 已删除时为 None）。
pub(crate) fn call_in(c: &Connection, parent_id: &SessionId, call_id: &str) -> Result<Option<CallRecord>> {
    Ok(query(c, "parent_session_id = ?1 AND call_id = ?2", params![parent_id, call_id])?.pop())
}

/// 这个会话里每次开出或续写 thread 的调用 → 那个 thread。
pub(crate) fn call_threads_in(c: &Connection, parent_id: &SessionId) -> Result<BTreeMap<String, SessionId>> {
    Ok(query(c, "parent_session_id = ?1", [parent_id])?.into_iter().map(|r| (r.call_id, r.thread_id)).collect())
}

/// 这些委派行（thread 已删除的不在结果里）。
pub(crate) fn delegations_among(c: &Connection, ids: &[DelegationId]) -> Result<BTreeMap<DelegationId, CallRecord>> {
    if ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let filter = format!("id IN ({})", placeholders(ids.len()));
    Ok(query(c, &filter, rusqlite::params_from_iter(ids))?.into_iter().map(|r| (r.id, r)).collect())
}

/// 还没结清的委派（发起方引用它的占用还 active），按占用登记先后；给了 ids 只看这些委派；session_id：只看这个会话发起的。
/// thread 已删除（占用引用已置空）的不在结果里。
pub(crate) fn owed_in(
    c: &Connection,
    ids: Option<&[DelegationId]>,
    session_id: Option<&SessionId>,
) -> Result<Vec<Owed>> {
    let held: Vec<(DelegationId, Hold)> = active_delegations_in(c, session_id)?
        .into_iter()
        .filter_map(|h| match h.of {
            HoldOf::Delegation { row: Some(row) } => Some((row, h)),
            _ => None,
        })
        .filter(|(row, _)| ids.is_none_or(|ids| ids.contains(row)))
        .collect();
    let rows: Vec<DelegationId> = held.iter().map(|(row, _)| *row).collect();
    let mut records = delegations_among(c, &rows)?;
    let mut owed = Vec::new();
    for (row, hold) in held {
        let Some(r) = records.remove(&row) else { continue };
        // 占用只在委派时登记（delegation::send_in）
        let CallMode::Delegated(kind) = r.mode else {
            return Err(broken(format!("hold {} refers to thread call {row}, which is not a delegation", hold.id)));
        };
        let delegation =
            Delegation { id: r.id, parent_id: r.parent_id, call_id: r.call_id, thread_id: r.thread_id, kind };
        owed.push(Owed { delegation, hold });
    }
    Ok(owed)
}
