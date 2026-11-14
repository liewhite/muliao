//! `triggers` 表的读写（只在本领域内用；HTTP 层与别的领域经 [`crate::service::TriggerService`]）。
//!
//! 一行读成 [`Trigger`]，改完整行写回（[`save_in`]）：各处只改结构体上自己负责的字段，写回一处实现。比较基线
//! `precheck_output` 可能很大、只有前置检查用，不随行读出（[`baseline_in`] / [`set_baseline_in`]）。

use chrono::{DateTime, Utc};
use muliao_platform::db::Tx;
use muliao_platform::ids::{AgentId, SessionId, TriggerId};
use rusqlite::types::Type;
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::faults::TriggerFault;
use crate::kinds::{Kind, Mode};
use crate::precheck::Precheck;
use crate::service::{Prompt, TriggerName};
use muliao_platform::errors::Result;

/// 一个触发器（一行）。
#[derive(Debug, Clone, PartialEq)]
pub struct Trigger {
    pub id: TriggerId,
    pub name: TriggerName,
    pub agent_id: AgentId,
    pub prompt: Prompt,
    /// 怎么触发：种类、配置与运行状态（kind、config、state 三列）
    pub mode: Mode,
    pub enabled: bool,
    pub last_fired_at: Option<DateTime<Utc>>,
    /// 上一次触发开出的会话（可能已删除）
    pub last_session_id: Option<SessionId>,
    /// 已为哪个挡路的会话（上一次触发开出、在等待）通知过用户，只经 [`crate::service::notify_waiting_in`] 改
    pub waiting_notified: Option<SessionId>,
    pub precheck: Option<Precheck>,
    pub last_checked_at: Option<DateTime<Utc>>,
    /// 脚本出错，只经 [`crate::faults`] 改
    pub faults: Vec<TriggerFault>,
    /// 不会再触发了（一次性触发器用掉了它那一次）
    pub finished_at: Option<DateTime<Utc>>,
    pub created_at: DateTime<Utc>,
}

const COLUMNS: &str = "id, name, agent_id, prompt, kind, config, state, enabled, last_fired_at, last_session_id, \
                       waiting_notified, precheck, last_checked_at, faults, finished_at, created_at";

fn json<T: DeserializeOwned>(row: &Row<'_>, i: usize) -> rusqlite::Result<T> {
    let text: String = row.get(i)?;
    serde_json::from_str(&text).map_err(|e| rusqlite::Error::FromSqlConversionFailure(i, Type::Text, Box::new(e)))
}

/// 一行读成 [`Trigger`]：种类、配置与状态在这里一次解析，读不懂就是这一行读不出来（整次查询报错）。
fn trigger_of(row: &Row<'_>) -> rusqlite::Result<Trigger> {
    let (kind, config, state): (Kind, String, String) = (row.get(4)?, row.get(5)?, row.get(6)?);
    let mode = Mode::from_columns(kind, &config, &state)
        .map_err(|e| rusqlite::Error::FromSqlConversionFailure(4, Type::Text, Box::new(e)))?;
    let prompt: Prompt = row.get(3)?;
    Ok(Trigger {
        id: row.get(0)?,
        name: row.get(1)?,
        agent_id: row.get(2)?,
        prompt,
        mode,
        enabled: row.get(7)?,
        last_fired_at: row.get(8)?,
        last_session_id: row.get(9)?,
        waiting_notified: row.get(10)?,
        precheck: match row.get::<_, Option<String>>(11)? {
            None => None,
            Some(_) => Some(json(row, 11)?),
        },
        last_checked_at: row.get(12)?,
        faults: json(row, 13)?,
        finished_at: row.get(14)?,
        created_at: row.get(15)?,
    })
}

fn to_text<T: Serialize>(value: &T) -> String {
    serde_json::to_string(value).expect("trigger columns serialize")
}

pub fn load_in(c: &Connection, trigger_id: TriggerId) -> Result<Option<Trigger>> {
    Ok(c.query_row(&format!("SELECT {COLUMNS} FROM triggers WHERE id = ?1"), [trigger_id], trigger_of).optional()?)
}

/// 同名的全部触发器（还会触发的至多一个，其余是已结束的历史）。
pub fn named_in(c: &Connection, name: &TriggerName) -> Result<Vec<Trigger>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM triggers WHERE name = ?1"))?;
    Ok(stmt.query_map([name], trigger_of)?.collect::<rusqlite::Result<_>>()?)
}

/// 列出哪些触发器。
#[derive(Debug, Clone, Default)]
pub struct Listing {
    pub kind: Option<Kind>,
    pub enabled_only: bool,
    /// 只列已结束的历史（按结束时间倒序）；否则只列还会触发的（按名称）
    pub finished: bool,
}

pub fn list_in(c: &Connection, listing: &Listing) -> Result<Vec<Trigger>> {
    let mut filter = if listing.finished { "finished_at IS NOT NULL" } else { "finished_at IS NULL" }.to_owned();
    if listing.enabled_only {
        filter.push_str(" AND enabled");
    }
    if listing.kind.is_some() {
        filter.push_str(" AND kind = ?1");
    }
    let order = if listing.finished { "finished_at DESC, id DESC" } else { "name" };
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM triggers WHERE {filter} ORDER BY {order}"))?;
    let rows = match listing.kind {
        Some(kind) => stmt.query_map([kind], trigger_of)?.collect::<rusqlite::Result<_>>()?,
        None => stmt.query_map([], trigger_of)?.collect::<rusqlite::Result<_>>()?,
    };
    Ok(rows)
}

/// 新建的一行（id、created_at 由库给出）。
pub struct NewTrigger {
    pub name: TriggerName,
    pub agent_id: AgentId,
    pub prompt: Prompt,
    pub mode: Mode,
    pub precheck: Option<Precheck>,
}

/// 插入；同名的未结束触发器已存在时返回 None。
pub fn insert_in(tx: &mut Tx<'_>, t: &NewTrigger) -> Result<Option<Trigger>> {
    let (kind, config, state) = t.mode.to_columns();
    let inserted = tx.execute(
        "INSERT INTO triggers (name, agent_id, prompt, kind, config, state, enabled, precheck, faults, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, ?7, '[]', ?8)",
        params![
            t.name,
            t.agent_id,
            t.prompt.as_str(),
            kind,
            config,
            state,
            t.precheck.as_ref().map(to_text),
            Utc::now()
        ],
    );
    match inserted {
        Ok(_) => load_in(tx, TriggerId::new(tx.last_insert_rowid())),
        Err(rusqlite::Error::SqliteFailure(e, _)) if e.code == rusqlite::ErrorCode::ConstraintViolation => Ok(None),
        Err(e) => Err(e.into()),
    }
}

/// 整行写回（id、created_at 不变）。
pub fn save_in(tx: &mut Tx<'_>, t: &Trigger) -> Result<()> {
    let (kind, config, state) = t.mode.to_columns();
    tx.execute(
        "UPDATE triggers SET name = ?1, agent_id = ?2, prompt = ?3, kind = ?4, config = ?5, state = ?6, enabled = ?7, \
         last_fired_at = ?8, last_session_id = ?9, precheck = ?10, last_checked_at = ?11, waiting_notified = ?12, \
         faults = ?13, finished_at = ?14 WHERE id = ?15",
        params![
            t.name,
            t.agent_id,
            t.prompt.as_str(),
            kind,
            config,
            state,
            t.enabled,
            t.last_fired_at,
            t.last_session_id,
            t.precheck.as_ref().map(to_text),
            t.last_checked_at,
            t.waiting_notified,
            to_text(&t.faults),
            t.finished_at,
            t.id
        ],
    )?;
    Ok(())
}

pub fn delete_in(tx: &mut Tx<'_>, trigger_id: TriggerId) -> Result<bool> {
    Ok(tx.execute("DELETE FROM triggers WHERE id = ?1", [trigger_id])? > 0)
}

/// changed 检查的比较基线（上一次成功检查的输出）。
pub fn baseline_in(c: &Connection, trigger_id: TriggerId) -> Result<Option<String>> {
    Ok(c.query_row("SELECT precheck_output FROM triggers WHERE id = ?1", [trigger_id], |r| r.get(0))
        .optional()?
        .flatten())
}

pub fn set_baseline_in(tx: &mut Tx<'_>, trigger_id: TriggerId, output: Option<&str>) -> Result<()> {
    tx.execute("UPDATE triggers SET precheck_output = ?1 WHERE id = ?2", params![output, trigger_id])?;
    Ok(())
}
