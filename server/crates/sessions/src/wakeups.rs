//! 到点唤醒（`wakeups` 的唯一读写处，docs/architecture.md 第 7 节）：会话用 wake_me 约一个时刻，到点时向它投一封事件信件
//! （带约的时候写的说明），它接着做。约好之后会话上有一个 active 占用（`wake:<会话>:<调用 id>`），所以工作状态是 waiting、
//! 委派来的工作不结清；到点由 `deliver_in` 交回并释放，和别的占用走同一个出口。
//!
//! - 一个会话至多一个还在等的唤醒：再约就取代（旧的占用放弃），也可以取消；会话被停止时占用被清掉，到点不投。
//! - 存在库里，重启不丢：到没到点只看 due_at 与占用的状态。

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use muliao_platform::db::{Database, Tx};
use rusqlite::{Connection, OptionalExtension, params};

use crate::actor::Waker;
use crate::error::Result;
use crate::holds::{self, COLUMN_COUNT, COLUMNS, Outside, abandon_in, from_row};
use crate::work::{EventSource, deliver_in};

/// 工具名，也是唤醒信件的事件来源
pub const WAKE_ME: &str = "wake_me";

pub struct Scheduled {
    pub due_at: DateTime<Utc>,
    /// 被这次取代的那个唤醒的时刻
    pub replaced: Option<DateTime<Utc>>,
}

/// 取消会话还在等的唤醒（至多一个），返回它原定的时刻。
pub fn cancel_in(c: &Connection, session_id: &str) -> Result<Option<DateTime<Utc>>> {
    let mut cancelled = None;
    for (hold, due_at) in waiting_in(c, "h.session_id = ?1", [session_id])? {
        abandon_in(c, &hold)?;
        cancelled = Some(due_at);
    }
    Ok(cancelled)
}

/// 占用还 active 的唤醒与它约的时刻（按约的时刻）；filter 是 SQL 条件（表别名 h 是占用、w 是唤醒）。
fn waiting_in(
    c: &Connection,
    filter: &str,
    params: impl rusqlite::Params,
) -> Result<Vec<(holds::Hold, DateTime<Utc>)>> {
    waiting_rows(c, filter, params, |r| r.get(COLUMN_COUNT))
}

/// 同 [`waiting_in`]，每行另取 extra（从第 [`COLUMN_COUNT`] 列起是 w.due_at、w.note、w.created_at）。
fn waiting_rows<T>(
    c: &Connection,
    filter: &str,
    params: impl rusqlite::Params,
    extra: impl Fn(&rusqlite::Row<'_>) -> rusqlite::Result<T>,
) -> Result<Vec<(holds::Hold, T)>> {
    let sql = format!(
        "SELECT {COLUMNS}, w.due_at, w.note, w.created_at FROM session_holds h JOIN wakeups w ON w.holder = h.holder \
         WHERE h.status = 'active' AND {filter} ORDER BY w.due_at"
    );
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt.query_map(params, |r| Ok((from_row(r)?, extra(r)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(|(raw, x)| Ok((holds::hold(raw)?, x))).collect()
}

/// 约一次唤醒，取代会话还在等的那个。awaited：用户是否在等约它的那次运行的回答（唤醒的信件随之推送）。同一次调用重放时
/// 返回已约的，不再取代。
pub fn schedule_in(
    c: &Connection,
    session_id: &str,
    call_id: &str,
    due_at: DateTime<Utc>,
    note: &str,
    awaited: bool,
) -> Result<Scheduled> {
    let of = Outside::Wake { call_id: call_id.to_owned() };
    let holder = of.holder(session_id);
    let existing: Option<DateTime<Utc>> =
        c.query_row("SELECT due_at FROM wakeups WHERE holder = ?1", [&holder], |r| r.get(0)).optional()?;
    if let Some(due_at) = existing {
        return Ok(Scheduled { due_at, replaced: None });
    }
    let replaced = cancel_in(c, session_id)?;
    holds::hold_in(c, session_id, of, awaited)?;
    c.execute(
        "INSERT INTO wakeups (holder, session_id, due_at, note, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![holder, session_id, due_at, note, Utc::now()],
    )?;
    Ok(Scheduled { due_at, replaced })
}

/// 会话还在等的唤醒（至多一个）：约的时刻与说明，会话页的等待项用。
pub(crate) fn pending_of_in(c: &Connection, session_id: &str) -> Result<Vec<(holds::Hold, DateTime<Utc>, String)>> {
    Ok(waiting_rows(c, "h.session_id = ?1", [session_id], |r| Ok((r.get(COLUMN_COUNT)?, r.get(COLUMN_COUNT + 1)?)))?
        .into_iter()
        .map(|(h, (due, note))| (h, due, note))
        .collect())
}

/// 给模型看的本地时刻（带时区缩写）。
pub fn local_time(at: DateTime<Utc>, tz: Tz) -> String {
    at.with_timezone(&tz).format("%Y-%m-%d %H:%M %Z").to_string()
}

/// 到点的唤醒：占用还 active、due_at 已到的，各投一封事件信件并释放占用。返回投了几封。
pub(crate) async fn deliver_due(db: &Database, waker: &Waker, tz: Tz) -> Result<usize> {
    let waker = waker.clone();
    db.write(move |tx: &mut Tx<'_>| {
        let now = Utc::now();
        let due = waiting_rows(tx, "w.due_at <= ?1", [now], |r| {
            Ok((
                r.get::<_, DateTime<Utc>>(COLUMN_COUNT)?,
                r.get::<_, String>(COLUMN_COUNT + 1)?,
                r.get(COLUMN_COUNT + 2)?,
            ))
        })?;
        for (hold, (due_at, note, created_at)) in &due {
            let created_at: &DateTime<Utc> = created_at;
            let text = format!(
                "Wake-up you scheduled with {WAKE_ME} at {} is due ({}). Your note: {note}",
                local_time(*created_at, tz),
                local_time(*due_at, tz)
            );
            deliver_in(tx, &waker, Some(hold), &EventSource::Wake, &text, None)?;
        }
        Ok(due.len())
    })
    .await
}
