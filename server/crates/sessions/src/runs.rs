//! 运行记录（`runs` 表的唯一读写处）：会话 actor 的一次运行，开始时写下、结束时记结局。信件交给了哪次
//! 运行、消息由哪次运行保存都指向它；没结束的就是停在半路（重启后接着跑）。最终失败记在这里，会话页据此显示错误与
//! 「重试」。写运行记录的都要给 [`Live`]：提交后发 `saved`，看着这个会话的页面重新拉取。

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, Row, params};

use muliao_platform::db::Tx;

use crate::live::Live;
use muliao_platform::db::placeholders;
use muliao_platform::errors::{Result, broken};

/// 一次运行的结局（[`end_in`] 记下）：失败的自带说明。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Done,
    Stopped,
    Failed(String),
}

impl Outcome {
    fn as_str(&self) -> &'static str {
        match self {
            Outcome::Done => "done",
            Outcome::Stopped => "stopped",
            Outcome::Failed(_) => "failed",
        }
    }

    fn error(&self) -> Option<&str> {
        match self {
            Outcome::Failed(e) => Some(e),
            _ => None,
        }
    }
}

/// 运行到了哪一步。
#[derive(Debug, Clone, PartialEq, serde::Serialize, utoipa::ToSchema)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum RunState {
    /// 在跑、停在半路（重启后接着跑）或在等重试
    Unfinished,
    Done {
        ended_at: DateTime<Utc>,
    },
    Stopped {
        ended_at: DateTime<Utc>,
    },
    /// 最终失败：会话页显示错误与「重试」
    Failed {
        ended_at: DateTime<Utc>,
        error: String,
    },
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, utoipa::ToSchema)]
pub struct RunRecord {
    pub id: i64,
    pub session_id: String,
    pub started_at: DateTime<Utc>,
    pub state: RunState,
}

impl RunRecord {
    pub(crate) fn unfinished(&self) -> bool {
        self.state == RunState::Unfinished
    }

    /// 最终失败的说明；没失败的为 None。
    pub(crate) fn failure(&self) -> Option<&str> {
        match &self.state {
            RunState::Failed { error, .. } => Some(error),
            _ => None,
        }
    }
}

const COLUMNS: &str = "id, session_id, started_at, ended_at, outcome, error";

type Raw = (i64, String, DateTime<Utc>, Option<DateTime<Utc>>, Option<String>, Option<String>);

fn raw(r: &Row<'_>) -> rusqlite::Result<Raw> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?))
}

fn record((id, session_id, started_at, ended_at, outcome, error): Raw) -> Result<RunRecord> {
    let state = match (ended_at, outcome.as_deref(), error) {
        (None, None, None) => RunState::Unfinished,
        (Some(ended_at), Some("done"), None) => RunState::Done { ended_at },
        (Some(ended_at), Some("stopped"), None) => RunState::Stopped { ended_at },
        (Some(ended_at), Some("failed"), Some(error)) => RunState::Failed { ended_at, error },
        (ended_at, outcome, error) => {
            return Err(broken(format!("run {id} has ended_at {ended_at:?}, outcome {outcome:?} and error {error:?}")));
        }
    };
    Ok(RunRecord { id, session_id, started_at, state })
}

fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<RunRecord>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM runs WHERE {filter} ORDER BY id"))?;
    let rows = stmt.query_map(params, raw)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(record).collect()
}

/// 会话没结束的那次运行（续跑）的编号；没有就开始一次新的。同一会话因此至多一个没结束的运行。
pub(crate) fn current_or_start_in(tx: &mut Tx<'_>, live: &Live, session_id: &str) -> Result<i64> {
    if let Some(r) = unfinished_in(tx, session_id)? {
        return Ok(r.id);
    }
    tx.execute("INSERT INTO runs (session_id, started_at) VALUES (?1, ?2)", params![session_id, Utc::now()])?;
    let id = tx.last_insert_rowid();
    live.saved_on_commit(tx, session_id);
    Ok(id)
}

/// 记下结局。
pub(crate) fn end_in(tx: &mut Tx<'_>, live: &Live, run_id: i64, outcome: Outcome) -> Result<()> {
    let session_id: Option<String> = tx
        .query_row(
            "UPDATE runs SET ended_at = ?1, outcome = ?2, error = ?3 WHERE id = ?4 AND ended_at IS NULL \
             RETURNING session_id",
            params![Utc::now(), outcome.as_str(), outcome.error(), run_id],
            |r| r.get(0),
        )
        .optional()?;
    let session_id = session_id.ok_or_else(|| broken(format!("run {run_id} ended twice or does not exist")))?;
    live.saved_on_commit(tx, &session_id);
    Ok(())
}

/// 手动重试一次最终失败的运行：它重新算作没结束（同一个编号，交给它的信与它保存的消息都还指向它）。
pub(crate) fn reopen_in(tx: &mut Tx<'_>, live: &Live, run: &RunRecord) -> Result<()> {
    tx.execute("UPDATE runs SET ended_at = NULL, outcome = NULL, error = NULL WHERE id = ?1", [run.id])?;
    live.saved_on_commit(tx, &run.session_id);
    Ok(())
}

/// 会话没结束的那次运行（至多一个）。
pub(crate) fn unfinished_in(c: &Connection, session_id: &str) -> Result<Option<RunRecord>> {
    Ok(query(c, "session_id = ?1 AND ended_at IS NULL", [session_id])?.into_iter().next())
}

/// 停止：没结束的运行记为停止（actor 已经放下它，或它停在两次重试之间）。
pub(crate) fn stop_unfinished_in(tx: &mut Tx<'_>, live: &Live, session_id: &str) -> Result<Vec<i64>> {
    let ids: Vec<i64> = query(tx, "session_id = ?1 AND ended_at IS NULL", [session_id])?.iter().map(|r| r.id).collect();
    for id in &ids {
        end_in(tx, live, *id, Outcome::Stopped)?;
    }
    Ok(ids)
}

/// 有没结束的运行的会话（重启后接着跑）。
pub(crate) fn sessions_unfinished_in(c: &Connection) -> Result<Vec<String>> {
    let mut stmt = c.prepare("SELECT DISTINCT session_id FROM runs WHERE ended_at IS NULL")?;
    Ok(stmt.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)
}

/// 这些会话里有没结束的运行的。
pub(crate) fn unfinished_among(c: &Connection, session_ids: &[String]) -> Result<Vec<String>> {
    if session_ids.is_empty() {
        return Ok(Vec::new());
    }
    let sql = format!(
        "SELECT DISTINCT session_id FROM runs WHERE ended_at IS NULL AND session_id IN ({})",
        placeholders(session_ids.len())
    );
    let mut stmt = c.prepare(&sql)?;
    Ok(stmt.query_map(rusqlite::params_from_iter(session_ids), |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)
}

/// 会话最近一次运行。
pub(crate) fn latest_in(c: &Connection, session_id: &str) -> Result<Option<RunRecord>> {
    Ok(latest_among(c, &[session_id.to_owned()])?.remove(session_id))
}

/// 这些会话各自最近一次运行；还没有运行的不在结果里。
pub(crate) fn latest_among(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, RunRecord>> {
    if session_ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let filter = format!(
        "id IN (SELECT max(id) FROM runs WHERE session_id IN ({}) GROUP BY session_id)",
        placeholders(session_ids.len())
    );
    Ok(query(c, &filter, rusqlite::params_from_iter(session_ids))?
        .into_iter()
        .map(|r| (r.session_id.clone(), r))
        .collect())
}

/// 一次运行。
pub fn get_in(c: &Connection, run_id: i64) -> Result<Option<RunRecord>> {
    let row = c.query_row(&format!("SELECT {COLUMNS} FROM runs WHERE id = ?1"), [run_id], raw).optional()?;
    row.map(record).transpose()
}

/// 这些会话在 [start, end) 里开始的运行，各自按先后。
pub(crate) fn started_between_in(
    c: &Connection,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Result<BTreeMap<String, Vec<RunRecord>>> {
    let mut out: BTreeMap<String, Vec<RunRecord>> = BTreeMap::new();
    for r in query(c, "started_at >= ?1 AND started_at < ?2", params![start, end])? {
        out.entry(r.session_id.clone()).or_default().push(r);
    }
    Ok(out)
}
