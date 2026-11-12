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
use crate::holds::{self, abandon_in, active_holders_of_in};
use crate::mailbox::placeholders;
use crate::work::{deliver_in, hold_of_in};

/// 工具名，也是唤醒信件的事件来源
pub const WAKE_ME: &str = "wake_me";

/// 约唤醒的那次调用在会话上的占用者：按调用 id 确定，续跑重新执行同一次调用时不重复约。
pub fn wake_holder(session_id: &str, call_id: &str) -> String {
    format!("wake:{session_id}:{call_id}")
}

pub struct Scheduled {
    pub due_at: DateTime<Utc>,
    /// 被这次取代的那个唤醒的时刻
    pub replaced: Option<DateTime<Utc>>,
}

/// 取消会话还在等的唤醒（至多一个），返回它原定的时刻。
pub fn cancel_in(c: &Connection, session_id: &str) -> Result<Option<DateTime<Utc>>> {
    let active = active_holders_of_in(c, session_id)?;
    if active.is_empty() {
        return Ok(None);
    }
    let sql = format!("SELECT holder, due_at FROM wakeups WHERE holder IN ({})", placeholders(active.len()));
    let rows: Vec<(String, DateTime<Utc>)> = {
        let mut stmt = c.prepare(&sql)?;
        stmt.query_map(rusqlite::params_from_iter(&active), |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?
    };
    let mut cancelled = None;
    for (holder, due_at) in rows {
        let hold = holds::get_in(c, session_id, &holder)?.expect("listed as an active holder");
        abandon_in(c, &hold)?;
        cancelled = Some(due_at);
    }
    Ok(cancelled)
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
    let holder = wake_holder(session_id, call_id);
    let existing: Option<DateTime<Utc>> =
        c.query_row("SELECT due_at FROM wakeups WHERE holder = ?1", [&holder], |r| r.get(0)).optional()?;
    if let Some(due_at) = existing {
        return Ok(Scheduled { due_at, replaced: None });
    }
    let replaced = cancel_in(c, session_id)?;
    holds::hold_in(c, session_id, &holder, awaited)?;
    c.execute(
        "INSERT INTO wakeups (holder, session_id, due_at, note, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![holder, session_id, due_at, note, Utc::now()],
    )?;
    Ok(Scheduled { due_at, replaced })
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
        let due: Vec<(String, String, DateTime<Utc>, String, DateTime<Utc>)> = {
            let mut stmt = tx.prepare(
                "SELECT w.holder, w.session_id, w.due_at, w.note, w.created_at FROM wakeups w \
                 JOIN session_holds h ON h.holder = w.holder AND h.status = 'active' \
                 WHERE w.due_at <= ?1 ORDER BY w.due_at",
            )?;
            stmt.query_map([now], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?
                .collect::<rusqlite::Result<_>>()?
        };
        for (holder, session_id, due_at, note, created_at) in &due {
            let text = format!(
                "Wake-up you scheduled with {WAKE_ME} at {} is due ({}). Your note: {note}",
                local_time(*created_at, tz),
                local_time(*due_at, tz)
            );
            let hold = hold_of_in(tx, session_id, holder)?;
            deliver_in(tx, &waker, hold.as_ref(), WAKE_ME, &text, None)?;
        }
        Ok(due.len())
    })
    .await
}
