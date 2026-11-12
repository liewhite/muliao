//! 运行记录（`runs` 表的唯一读写处）：会话 actor 的一次运行或一次手动压缩，开始时写下、结束时记结局。信件交给了哪次
//! 运行、消息由哪次运行保存都指向它；没结束的就是停在半路（重启后接着跑）。最终失败记在这里，会话页据此显示错误与
//! 「重试」。写运行记录的都要给 [`Live`]：提交后发 `saved`，看着这个会话的页面重新拉取。

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, Row, params};

use muliao_platform::db::Tx;

use crate::error::{Result, broken};
use crate::live::Live;
use crate::mailbox::placeholders;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum RunKind {
    Run,
    /// 用户在会话页手动压缩
    Compaction,
}

impl RunKind {
    pub fn as_str(self) -> &'static str {
        match self {
            RunKind::Run => "run",
            RunKind::Compaction => "compaction",
        }
    }

    fn parse(s: &str) -> Result<Self> {
        match s {
            "run" => Ok(RunKind::Run),
            "compaction" => Ok(RunKind::Compaction),
            _ => Err(broken(format!("unknown run kind {s:?}"))),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Done,
    Stopped,
    Failed,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Done => "done",
            Outcome::Stopped => "stopped",
            Outcome::Failed => "failed",
        }
    }

    fn parse(s: &str) -> Result<Self> {
        match s {
            "done" => Ok(Outcome::Done),
            "stopped" => Ok(Outcome::Stopped),
            "failed" => Ok(Outcome::Failed),
            _ => Err(broken(format!("unknown run outcome {s:?}"))),
        }
    }
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, utoipa::ToSchema)]
pub struct RunRecord {
    pub id: i64,
    pub session_id: String,
    pub kind: RunKind,
    pub started_at: DateTime<Utc>,
    pub ended_at: Option<DateTime<Utc>>,
    pub outcome: Option<Outcome>,
    pub error: Option<String>,
}

impl RunRecord {
    pub fn failed(&self) -> bool {
        self.outcome == Some(Outcome::Failed)
    }

    /// 最终失败的说明。
    pub fn failure(&self) -> Option<&str> {
        if self.failed() { self.error.as_deref() } else { None }
    }
}

const COLUMNS: &str = "id, session_id, kind, started_at, ended_at, outcome, error";

type Raw = (i64, String, String, DateTime<Utc>, Option<DateTime<Utc>>, Option<String>, Option<String>);

fn raw(r: &Row<'_>) -> rusqlite::Result<Raw> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?))
}

fn record((id, session_id, kind, started_at, ended_at, outcome, error): Raw) -> Result<RunRecord> {
    Ok(RunRecord {
        id,
        session_id,
        kind: RunKind::parse(&kind)?,
        started_at,
        ended_at,
        outcome: outcome.as_deref().map(Outcome::parse).transpose()?,
        error,
    })
}

fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<RunRecord>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM runs WHERE {filter} ORDER BY id"))?;
    let rows = stmt.query_map(params, raw)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(record).collect()
}

/// 开始一次运行，返回它的编号。同一会话同时只有一个没结束的（actor 串行），已有就是 bug。
pub fn start_in(tx: &mut Tx<'_>, live: &Live, session_id: &str, kind: RunKind) -> Result<i64> {
    if kind == RunKind::Run
        && let Some(r) = unfinished_in(tx, session_id)?
    {
        panic!("session {session_id} starts a run while run {} is unfinished", r.id);
    }
    tx.execute(
        "INSERT INTO runs (session_id, kind, started_at) VALUES (?1, ?2, ?3)",
        params![session_id, kind.as_str(), Utc::now()],
    )?;
    let id = tx.last_insert_rowid();
    live.saved_on_commit(tx, session_id);
    Ok(id)
}

/// 记下结局。
pub fn end_in(tx: &mut Tx<'_>, live: &Live, run_id: i64, outcome: Outcome, error: Option<&str>) -> Result<()> {
    let session_id: Option<String> = tx
        .query_row(
            "UPDATE runs SET ended_at = ?1, outcome = ?2, error = ?3 WHERE id = ?4 AND ended_at IS NULL \
             RETURNING session_id",
            params![Utc::now(), outcome.as_str(), error, run_id],
            |r| r.get(0),
        )
        .optional()?;
    let session_id = session_id.ok_or_else(|| broken(format!("run {run_id} ended twice or does not exist")))?;
    live.saved_on_commit(tx, &session_id);
    Ok(())
}

/// 手动重试一次最终失败的运行：它重新算作没结束（同一个编号，交给它的信与它保存的消息都还指向它）。
pub fn reopen_in(tx: &mut Tx<'_>, live: &Live, run: &RunRecord) -> Result<()> {
    tx.execute("UPDATE runs SET ended_at = NULL, outcome = NULL, error = NULL WHERE id = ?1", [run.id])?;
    live.saved_on_commit(tx, &run.session_id);
    Ok(())
}

/// 会话没结束的那次运行（至多一个）。
pub fn unfinished_in(c: &Connection, session_id: &str) -> Result<Option<RunRecord>> {
    Ok(query(c, "session_id = ?1 AND kind = 'run' AND ended_at IS NULL", [session_id])?.into_iter().next())
}

/// 停止：没结束的运行记为停止（actor 已经放下它，或它停在两次重试之间）。
pub fn stop_unfinished_in(tx: &mut Tx<'_>, live: &Live, session_id: &str) -> Result<Vec<i64>> {
    let ids: Vec<i64> = query(tx, "session_id = ?1 AND kind = 'run' AND ended_at IS NULL", [session_id])?
        .iter()
        .map(|r| r.id)
        .collect();
    for id in &ids {
        end_in(tx, live, *id, Outcome::Stopped, None)?;
    }
    Ok(ids)
}

/// 服务启动时：停在半路的手动压缩（只在内存里排，不接着做）记为失败。「没结束的运行」的查询都只认普通运行。
pub fn abandon_compactions_in(c: &Connection) -> Result<usize> {
    Ok(c.execute(
        "UPDATE runs SET ended_at = ?1, outcome = 'failed', error = 'interrupted by a service restart' \
         WHERE kind = 'compaction' AND ended_at IS NULL",
        [Utc::now()],
    )?)
}

/// 有没结束的运行的会话（重启后接着跑）。
pub fn sessions_unfinished_in(c: &Connection) -> Result<Vec<String>> {
    let mut stmt = c.prepare("SELECT DISTINCT session_id FROM runs WHERE kind = 'run' AND ended_at IS NULL")?;
    Ok(stmt.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)
}

/// 这些会话里有没结束的运行的。
pub fn unfinished_among(c: &Connection, session_ids: &[String]) -> Result<Vec<String>> {
    if session_ids.is_empty() {
        return Ok(Vec::new());
    }
    let sql = format!(
        "SELECT DISTINCT session_id FROM runs WHERE kind = 'run' AND ended_at IS NULL AND session_id IN ({})",
        placeholders(session_ids.len())
    );
    let mut stmt = c.prepare(&sql)?;
    Ok(stmt.query_map(rusqlite::params_from_iter(session_ids), |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)
}

/// 会话最近一次运行（不算手动压缩）。
pub fn latest_in(c: &Connection, session_id: &str) -> Result<Option<RunRecord>> {
    Ok(latest_among(c, &[session_id.to_owned()])?.remove(session_id))
}

/// 这些会话各自最近一次运行（不算手动压缩）；还没有运行的不在结果里。
pub fn latest_among(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, RunRecord>> {
    if session_ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let filter = format!(
        "id IN (SELECT max(id) FROM runs WHERE kind = 'run' AND session_id IN ({}) GROUP BY session_id)",
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
pub fn started_between_in(
    c: &Connection,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Result<BTreeMap<String, Vec<RunRecord>>> {
    let mut out: BTreeMap<String, Vec<RunRecord>> = BTreeMap::new();
    for r in query(c, "kind = 'run' AND started_at >= ?1 AND started_at < ?2", params![start, end])? {
        out.entry(r.session_id.clone()).or_default().push(r);
    }
    Ok(out)
}
