//! 等用户批准的工具调用（`approval_requests` 的唯一读写处，#530）：工具执行前要用户批准（如 skill_run 执行 SKILL.md 里
//! 标了 approval 的脚本），运行停在这里正常结束；用户批准或拒绝，决定作为回答信件交回，批准后工具按原参数执行。
//!
//! 等批准的调用不按工具名认（同一个工具多数调用不用批准）：运行停下时把这些调用记下，没有结果、记过的调用就是在等批准。
//! 「始终允许」由工具自己记（批准随 agenter 的 `Approval::always` 交回工具）。

use std::collections::{BTreeMap, HashMap, HashSet};

use agenter::Resolution;
use chrono::Utc;
use rusqlite::{Connection, OptionalExtension, params};

use crate::answers::{APPROVAL_INTERRUPTED, CallResult};
use crate::error::Result;
use crate::mailbox::placeholders;

/// 这些调用停下来等批准了（运行收尾的事务里），连同工具请求批准时定下的 pin。已记过的不再记。
pub fn record_in(c: &Connection, session_id: &str, calls: &[(String, String)]) -> Result<()> {
    for (call_id, pin) in calls {
        c.execute(
            "INSERT OR IGNORE INTO approval_requests (session_id, call_id, pin, created_at) VALUES (?1, ?2, ?3, ?4)",
            params![session_id, call_id, pin, Utc::now()],
        )?;
    }
    Ok(())
}

/// 交给运行的回答里被批准的调用要去执行了（运行开头取走信件的事务里）：没交过的记下时间。之前已经交过的，那次运行可能在
/// 执行中被打断，不能再执行一遍：返回它们的结果「被中断」，代替批准交给模型。
pub fn take_in(
    c: &Connection,
    session_id: &str,
    answers: &HashMap<String, CallResult>,
) -> Result<HashMap<String, Resolution>> {
    let mut interrupted = HashMap::new();
    for (call_id, r) in answers {
        let CallResult::Approval(d) = r else { continue };
        if !d.approved {
            continue;
        }
        let taken: Option<Option<String>> = c
            .query_row(
                "SELECT taken_at FROM approval_requests WHERE session_id = ?1 AND call_id = ?2",
                params![session_id, call_id],
                |r| r.get(0),
            )
            .optional()?;
        match taken {
            Some(Some(_)) => {
                interrupted.insert(call_id.clone(), Resolution::Denied(APPROVAL_INTERRUPTED.to_owned()));
            }
            Some(None) => {
                c.execute(
                    "UPDATE approval_requests SET taken_at = ?1 WHERE session_id = ?2 AND call_id = ?3",
                    params![Utc::now(), session_id, call_id],
                )?;
            }
            None => {}
        }
    }
    Ok(interrupted)
}

/// 这些会话里停下来等过批准的调用 id（不论现在有没有结果），每个会话都在结果里。
pub fn requested_among(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, HashSet<String>>> {
    let mut found: BTreeMap<String, HashSet<String>> =
        session_ids.iter().map(|s| (s.clone(), HashSet::new())).collect();
    if session_ids.is_empty() {
        return Ok(found);
    }
    let sql = format!(
        "SELECT session_id, call_id FROM approval_requests WHERE session_id IN ({})",
        placeholders(session_ids.len())
    );
    let mut stmt = c.prepare(&sql)?;
    for row in stmt.query_map(rusqlite::params_from_iter(session_ids), |r| Ok((r.get::<_, String>(0)?, r.get(1)?)))? {
        let (s, call) = row?;
        found.entry(s).or_default().insert(call);
    }
    Ok(found)
}

/// 这些等批准的调用请求批准时记下的 pin（用户提交决定时记进决定）。
pub fn pins_in(c: &Connection, session_id: &str, call_ids: &[String]) -> Result<HashMap<String, Option<String>>> {
    let mut pins = HashMap::new();
    for id in call_ids {
        let pin: Option<Option<String>> = c
            .query_row(
                "SELECT pin FROM approval_requests WHERE session_id = ?1 AND call_id = ?2",
                params![session_id, id],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(pin) = pin {
            pins.insert(id.clone(), pin);
        }
    }
    Ok(pins)
}
