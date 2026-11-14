//! 在会话之外进行的工作与会话的接口（docs/architecture.md 第 7 节「占用与结清」）：登记占用、运行中检查、交回结局。
//!
//! - [`SessionWork`]：别的领域（后台任务）在自己的事务里登记占用与没开始就放弃、交回结局与运行中检查。
//! - [`deliver_in`]：交回结局并释放占用，和一封信件同一个事务；占用已被停止清掉、或会话已删除时不投。唯一出口。
//! - [`post_check_in`]：运行中检查投信（不推送），这个来源已有信在排着、或占用不再 active 时不投。

use muliao_platform::ids::{JobId, SessionId};
use std::collections::BTreeMap;

use muliao_platform::db::Tx;
use rusqlite::Connection;

use crate::actor::Waker;
use crate::answers::CallResult;
use crate::directory;
use crate::history::since_last_response;
use crate::holds::{self, Hold, HoldStatus};
use crate::mailbox::{NewMail, has_queued_from, post_in, queued_in};
use crate::paused_calls::kinds_in;
use crate::pending::{Position, position};
use muliao_platform::errors::{Result, broken};

/// 发起并发工作时不给 heartbeat_seconds 的默认间隔
pub const DEFAULT_CHECK_SECONDS: f64 = 60.0;

/// 工具参数 heartbeat_seconds → 运行中检查的间隔（run_shell(background)、delegate_task(report_back) 共用）：不给时取默认，
/// 不大于 0（含 NaN）时是参数错误。
pub fn check_interval(heartbeat_seconds: Option<f64>) -> Result<f64, String> {
    let interval = heartbeat_seconds.unwrap_or(DEFAULT_CHECK_SECONDS);
    if interval > 0.0 { Ok(interval) } else { Err("heartbeat_seconds must be greater than 0".to_owned()) }
}

/// 会话现在停在哪（规则在 [`position`]）。没出结果的调用只可能在最后一条回复里，不加载整段上下文。
pub(crate) fn position_in(c: &Connection, session_id: &SessionId) -> Result<Position> {
    position(&since_last_response(c, session_id)?, &kinds_in(c, session_id)?, &queued_in(c, session_id)?)
}

/// 交回结局的结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Delivered {
    /// 作为挂起调用的结果（回答信件）
    Answer,
    /// 作为事件信件
    Event,
    /// 占用已被停止清掉：不投
    Dropped,
}

/// 交回这项占用的结局并释放它，唯一出口。answer 是（调用 id, 结果）：会话还停在这次调用上时作为那次调用的结果；否则作为
/// 事件信件（「迟到的回答改为事件」只在这里）。已经释放过的再交回是 bug，当场报错。
pub(crate) fn deliver_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    hold: &Hold,
    source: &EventSource,
    event_text: &str,
    answer: Option<(&str, CallResult)>,
) -> Result<Delivered> {
    let session_id = &hold.session_id;
    match holds::release_in(tx, hold)? {
        HoldStatus::Cleared => {
            tracing::info!(%session_id, hold_id = %hold.id, "outcome for a stopped session dropped");
            return Ok(Delivered::Dropped);
        }
        HoldStatus::Released => {
            return Err(broken(format!("hold {} of session {session_id} was already released", hold.id)));
        }
        HoldStatus::Active => {}
    }
    let at = position_in(tx, session_id)?;
    let delivered = match answer {
        Some((call_id, answer)) if at.paused().is_some_and(|p| p.get(call_id).is_some()) => {
            post_in(tx, waker, session_id, NewMail::answer(call_id.to_owned(), answer, hold.awaited))?;
            Delivered::Answer
        }
        _ => {
            post_in(tx, waker, session_id, NewMail::event(source.clone(), event_text, false, hold.awaited))?;
            Delivered::Event
        }
    };
    tracing::info!(%session_id, hold_id = %hold.id, delivered_as = ?delivered, "outcome delivered");
    Ok(delivered)
}

/// 运行中检查：给这项工作的会话投一封检查信件（不推送）。这个来源已有信件排着时不投（不堆积）；占用已不是 active 时也
/// 不投。返回是否投了。
pub(crate) fn post_check_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    hold: &Hold,
    source: &EventSource,
    text: &str,
) -> Result<bool> {
    if hold.status != HoldStatus::Active {
        return Ok(false);
    }
    if has_queued_from(tx, &hold.session_id, &source.raw())? {
        return Ok(false);
    }
    post_in(tx, waker, &hold.session_id, NewMail::event(source.clone(), text, true, false))?;
    Ok(true)
}

/// 后台任务的占用（交回结局与运行中检查用）：会话已删除时 None（占用随会话删掉）；会话还在而占用从未登记过是 bug。
fn job_hold_in(c: &Connection, session_id: &SessionId, job_id: JobId) -> Result<Option<Hold>> {
    let hold = holds::job_in(c, job_id)?;
    if hold.is_none() && directory::get_in(c, session_id)?.is_some() {
        return Err(broken(format!("session {session_id} has no hold for job {job_id}")));
    }
    Ok(hold)
}

/// 到点唤醒的事件来源的写法（存在信件里，也交给模型）。与工具名 wake_me 同字只是巧合：工具改名不改它，已存的信件靠它认
const WAKE_SOURCE: &str = "wake_me";

/// 事件信件的来源：交给模型时套在事件外面（[`EventSource::raw`]），界面按种类显示。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EventSource {
    /// 后台任务
    Job { job_id: JobId },
    /// thread 交回的结果或运行中检查
    Thread { session_id: SessionId },
    /// 到点唤醒
    Wake,
}

impl EventSource {
    /// 存下与交给模型的写法（`job:7`、`thread:<会话 id>`、`wake_me`）。
    pub fn raw(&self) -> String {
        match self {
            EventSource::Job { job_id } => format!("job:{job_id}"),
            EventSource::Thread { session_id } => format!("thread:{session_id}"),
            EventSource::Wake => WAKE_SOURCE.to_owned(),
        }
    }

    /// [`EventSource::raw`] 的逆运算（读库边界用）；不是三种写法之一为 None。
    pub(crate) fn parse(raw: &str) -> Option<Self> {
        if raw == WAKE_SOURCE {
            return Some(EventSource::Wake);
        }
        if let Some(id) = raw.strip_prefix("thread:").filter(|s| !s.is_empty()) {
            return Some(EventSource::Thread { session_id: SessionId::new(id) });
        }
        let digits = raw.strip_prefix("job:").filter(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))?;
        Some(EventSource::Job { job_id: JobId::new(digits.parse().ok()?) })
    }
}

/// 别的领域在会话之外做一项工作时对会话的命令（都在发送方自己的事务里）。
#[derive(Clone)]
pub struct SessionWork {
    waker: Waker,
}

impl SessionWork {
    pub fn new(waker: Waker) -> Self {
        Self { waker }
    }

    /// 登记一项进行中的后台任务：会话据此知道工作还没做完。awaited：用户是否在等它的结局。会话不存在（刚被删除）时占用
    /// 的外键让插入失败，调用方的事务随之回滚。
    pub fn hold_in(&self, tx: &mut Tx<'_>, session_id: &SessionId, job_id: JobId, awaited: bool) -> Result<()> {
        holds::hold_job_in(tx, session_id, job_id, awaited)?;
        Ok(())
    }

    /// 后台任务结束了：往会话的信箱投一封事件信件，同时释放占用；会话已删除时不投。
    pub fn ended_in(&self, tx: &mut Tx<'_>, session_id: &SessionId, job_id: JobId, text: &str) -> Result<()> {
        let Some(hold) = job_hold_in(tx, session_id, job_id)? else {
            tracing::info!(%session_id, %job_id, "outcome of a job for a deleted session dropped");
            return Ok(());
        };
        deliver_in(tx, &self.waker, &hold, &EventSource::Job { job_id }, text, None)?;
        Ok(())
    }

    /// 后台任务还在进行：给会话投一封检查信件；会话已删除时不投。返回是否投了。
    pub fn checked_in(&self, tx: &mut Tx<'_>, session_id: &SessionId, job_id: JobId, text: &str) -> Result<bool> {
        match job_hold_in(tx, session_id, job_id)? {
            Some(hold) => post_check_in(tx, &self.waker, &hold, &EventSource::Job { job_id }, text),
            None => Ok(false),
        }
    }
}

/// 收到运行中检查、没有要做的事时只回的占位答复（唯一一份：检查信件与工具说明教 agent 这样回，界面按
/// [`is_no_progress`] 认出它并收起）。
pub const NO_PROGRESS_REPLY: &str = "…";

/// 教 agent 怎么回运行中检查的那半句（检查信件与工具说明共用）。
pub fn no_progress_hint() -> String {
    format!("if nothing needs doing, reply with just '{NO_PROGRESS_REPLY}'")
}

/// 这段回答只是占位答复（模型有时写成三个点）。
pub(crate) fn is_no_progress(text: &str) -> bool {
    matches!(text.trim(), "" | NO_PROGRESS_REPLY | "...")
}

/// 运行中检查（后台任务、委派）与到点唤醒多久看一次（唯一一份）。
pub const TICK: std::time::Duration = std::time::Duration::from_secs(5);

/// 运行中检查的节拍：每项工作上一次检查的时刻（还没检查过的以工作开始的时刻算）。只是节拍，重启后从开始的时刻重新算。
#[derive(Debug)]
pub struct CheckBeats<K: Ord + Clone> {
    last: BTreeMap<K, chrono::DateTime<chrono::Utc>>,
}

impl<K: Ord + Clone> Default for CheckBeats<K> {
    fn default() -> Self {
        Self { last: BTreeMap::new() }
    }
}

impl<K: Ord + Clone> CheckBeats<K> {
    /// 只留还在被检查的工作。
    pub fn keep(&mut self, watched: &std::collections::BTreeSet<K>) {
        self.last.retain(|k, _| watched.contains(k));
    }

    /// 上一次检查之后过了一个间隔。
    pub fn due(
        &self,
        key: &K,
        started: chrono::DateTime<chrono::Utc>,
        interval_seconds: f64,
        now: chrono::DateTime<chrono::Utc>,
    ) -> bool {
        let since = *self.last.get(key).unwrap_or(&started);
        (now - since).num_milliseconds() as f64 >= interval_seconds * 1000.0
    }

    pub fn checked(&mut self, key: K, now: chrono::DateTime<chrono::Utc>) {
        self.last.insert(key, now);
    }
}
