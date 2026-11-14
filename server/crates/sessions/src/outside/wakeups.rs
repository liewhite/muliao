//! 到点唤醒（`wakeups` 的唯一读写处，docs/architecture.md 第 7 节）：会话用 wake_me 约一个时刻，到点时向它投一封事件信件
//! （带约的时候写的说明），它接着做。约好之后会话上有一个 active 占用（每行唤醒引用它），所以工作状态是 waiting、
//! 委派来的工作不结清；到点由 `deliver_in` 交回并释放，和别的占用走同一个出口。
//!
//! - 一个会话至多一个还在等的唤醒：再约就取代（旧的占用放弃），也可以取消；会话被停止时占用被清掉，到点不投。
//! - 存在库里，重启不丢：到没到点只看 due_at 与占用的状态。

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use muliao_platform::db::{Database, Tx};
use muliao_platform::ids::SessionId;
use rusqlite::{Connection, params};

use crate::execution::actor::Waker;
use crate::mailbox::EventSource;
use crate::outside::holds::{self, Hold, abandon_in, active_wakes_in};
use crate::outside::work::deliver_in;
use muliao_platform::errors::Result;

/// 工具名
pub(crate) const WAKE_ME: &str = "wake_me";

pub(crate) struct Scheduled {
    pub due_at: DateTime<Utc>,
    /// 被这次取代的那个唤醒的时刻
    pub replaced: Option<DateTime<Utc>>,
}

/// 约好的一次唤醒。
struct Wakeup {
    due_at: DateTime<Utc>,
    note: String,
    created_at: DateTime<Utc>,
}

/// 取消会话还在等的唤醒（至多一个），返回它原定的时刻。
pub(crate) fn cancel_in(c: &Tx<'_>, session_id: &SessionId) -> Result<Option<DateTime<Utc>>> {
    let mut cancelled = None;
    for (hold, wakeup) in waiting_in(c, Some(session_id))? {
        abandon_in(c, &hold)?;
        cancelled = Some(wakeup.due_at);
    }
    Ok(cancelled)
}

/// 占用还 active 的唤醒（按约的时刻）；session_id：只看这个会话的。
fn waiting_in(c: &Connection, session_id: Option<&SessionId>) -> Result<Vec<(Hold, Wakeup)>> {
    let mut stmt = c.prepare("SELECT due_at, note, created_at FROM wakeups WHERE hold_id = ?1")?;
    let mut waiting = Vec::new();
    for hold in active_wakes_in(c, session_id)? {
        let wakeup =
            stmt.query_row([hold.id], |r| Ok(Wakeup { due_at: r.get(0)?, note: r.get(1)?, created_at: r.get(2)? }))?;
        waiting.push((hold, wakeup));
    }
    waiting.sort_by_key(|(_, w)| w.due_at);
    Ok(waiting)
}

/// 约一次唤醒，取代会话还在等的那个。awaited：用户是否在等约它的那次运行的回答（唤醒的信件随之推送）。
pub(crate) fn schedule_in(
    c: &Tx<'_>,
    session_id: &SessionId,
    due_at: DateTime<Utc>,
    note: &str,
    awaited: bool,
) -> Result<Scheduled> {
    let replaced = cancel_in(c, session_id)?;
    let hold = holds::hold_wake_in(c, session_id, awaited)?;
    c.execute(
        "INSERT INTO wakeups (hold_id, due_at, note, created_at) VALUES (?1, ?2, ?3, ?4)",
        params![hold.id, due_at, note, Utc::now()],
    )?;
    Ok(Scheduled { due_at, replaced })
}

/// 会话还在等的唤醒（至多一个）：约的时刻与说明，会话页的等待项用。
pub(crate) fn pending_of_in(c: &Connection, session_id: &SessionId) -> Result<Vec<(Hold, DateTime<Utc>, String)>> {
    Ok(waiting_in(c, Some(session_id))?.into_iter().map(|(h, w)| (h, w.due_at, w.note)).collect())
}

/// 给模型看的本地时刻（带时区缩写）。
pub(crate) fn local_time(at: DateTime<Utc>, tz: Tz) -> String {
    at.with_timezone(&tz).format("%Y-%m-%d %H:%M %Z").to_string()
}

/// 到点的唤醒：占用还 active、due_at 已到的，各投一封事件信件并释放占用。返回投了几封。
pub(crate) async fn deliver_due(db: &Database, waker: &Waker, tz: Tz) -> Result<usize> {
    let waker = waker.clone();
    db.write(move |tx: &mut Tx<'_>| {
        let now = Utc::now();
        let due: Vec<_> = waiting_in(tx, None)?.into_iter().take_while(|(_, w)| w.due_at <= now).collect();
        for (hold, wakeup) in &due {
            let text = format!(
                "Wake-up you scheduled with {WAKE_ME} at {} is due ({}). Your note: {}",
                local_time(wakeup.created_at, tz),
                local_time(wakeup.due_at, tz),
                wakeup.note
            );
            deliver_in(tx, &waker, hold, &EventSource::Wake, &text, None)?;
        }
        Ok(due.len())
    })
    .await
}
