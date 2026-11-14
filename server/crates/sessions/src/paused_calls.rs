//! 挂起的调用（`paused_calls` 的唯一读写处，#823）：一次调用停下来等回信时，在它挂起的那个事务里记下一行，写明是哪一种
//! （问用户、问 agent、等批准）。之后判断会话停在哪、停止时怎么收尾、续跑时补什么，都只看这里，不按工具名认。行永久保留：
//! 没出结果的调用有行就是在等，没有行就是执行到一半被打断。
//!
//! 谁在什么时候记：ask_user 由工具自己在挂起前记；ask_agent 在往 thread 投信的同一事务里记（thread 可能在发问方这一步
//! 保存完之前就答完，交回时要认得出这是挂起调用的回答）；等批准的调用工具还没执行，在运行停下保存的事务里记，连同工具
//! 请求批准时定下的 pin。批准第一次交给运行去执行时记下时间，再交就是可能执行过一半，改给「被中断」。

use muliao_platform::ids::SessionId;
use std::collections::{BTreeMap, HashMap};

use agenter::Resolution;
use chrono::Utc;
use rusqlite::{Connection, OptionalExtension, params};

use crate::answers::{APPROVAL_INTERRUPTED, CallResult, Waiting};
use muliao_platform::db::placeholders;
use muliao_platform::errors::{Result, broken};

/// 一个会话里挂起过的调用（调用 id → 种类）；行永久保留，没结果的才在等。
pub(crate) type PausedKinds = HashMap<String, Waiting>;

/// 一次调用挂起成哪一种。等批准独有被批准对象的指纹 pin。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Pause {
    AskUser,
    AskAgent,
    /// pin：工具请求批准时定下的被批准对象的指纹，批准时交回工具比对
    Approval {
        pin: String,
    },
}

impl Pause {
    fn kind(&self) -> Waiting {
        match self {
            Pause::AskUser => Waiting::AskUser,
            Pause::AskAgent => Waiting::AskAgent,
            Pause::Approval { .. } => Waiting::Approval,
        }
    }
}

/// 这次调用挂起了。每次调用只挂起一次（被打断的调用不重做），表上的唯一索引挡住重复。
pub(crate) fn record_in(c: &Connection, session_id: &SessionId, call_id: &str, pause: &Pause) -> Result<()> {
    let pin = match pause {
        Pause::Approval { pin } => Some(pin.as_str()),
        Pause::AskUser | Pause::AskAgent => None,
    };
    c.execute(
        "INSERT INTO paused_calls (session_id, call_id, kind, pin, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![session_id, call_id, pause.kind().as_str(), pin, Utc::now()],
    )?;
    Ok(())
}

/// 这些会话里挂起过的调用（不论现在有没有结果），每个会话都在结果里。
pub(crate) fn kinds_among(c: &Connection, session_ids: &[SessionId]) -> Result<BTreeMap<SessionId, PausedKinds>> {
    let mut found: BTreeMap<SessionId, PausedKinds> = session_ids.iter().map(|s| (s.clone(), HashMap::new())).collect();
    if session_ids.is_empty() {
        return Ok(found);
    }
    let sql = format!(
        "SELECT session_id, call_id, kind FROM paused_calls WHERE session_id IN ({})",
        placeholders(session_ids.len())
    );
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(session_ids), |r| {
        Ok((r.get::<_, SessionId>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?))
    })?;
    for row in rows {
        let (s, call, kind) = row?;
        // 列上有 CHECK 约束，只会是这三种
        let kind =
            Waiting::parse(&kind).ok_or_else(|| broken(format!("paused call {call} has unknown kind {kind}")))?;
        found.entry(s).or_default().insert(call, kind);
    }
    Ok(found)
}

/// 一个会话里挂起过的调用。
pub(crate) fn kinds_in(c: &Connection, session_id: &SessionId) -> Result<PausedKinds> {
    Ok(kinds_among(c, std::slice::from_ref(session_id))?.remove(session_id).unwrap_or_default())
}

/// 交给运行的回答里被批准的调用要去执行了（运行开头取走信件的事务里）：没交过的记下时间。之前已经交过的，那次运行可能在
/// 执行中被打断，不能再执行一遍：返回它们的结果「被中断」，代替批准交给模型。
pub(crate) fn take_in(
    c: &Connection,
    session_id: &SessionId,
    answers: &HashMap<String, CallResult>,
) -> Result<HashMap<String, Resolution>> {
    let mut interrupted = HashMap::new();
    for (call_id, r) in answers {
        let CallResult::Approval(d) = r else { continue };
        if !d.approved() {
            continue;
        }
        let taken: Option<Option<String>> = c
            .query_row(
                "SELECT taken_at FROM paused_calls WHERE session_id = ?1 AND call_id = ?2 AND kind = 'approval'",
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
                    "UPDATE paused_calls SET taken_at = ?1 WHERE session_id = ?2 AND call_id = ?3",
                    params![Utc::now(), session_id, call_id],
                )?;
            }
            // 回答按挂起记录的种类配对（deferred_results），走到这里是记录被删了或数据对不上
            None => return Err(broken(format!("approved call {call_id} has no approval recorded"))),
        }
    }
    Ok(interrupted)
}

/// 这个等批准的调用请求批准时记下的 pin（用户提交决定时记进决定）。
pub(crate) fn pin_in(c: &Connection, session_id: &SessionId, call_id: &str) -> Result<String> {
    Ok(c.query_row(
        "SELECT pin FROM paused_calls WHERE session_id = ?1 AND call_id = ?2 AND kind = 'approval'",
        params![session_id, call_id],
        |r| r.get(0),
    )?)
}
