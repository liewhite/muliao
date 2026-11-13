//! 在会话之外进行的工作与会话的接口（docs/architecture.md 第 7 节「占用与结清」）：登记占用、运行中检查、交回结局。
//!
//! - [`SessionWork`]：别的领域（后台任务）在自己的事务里登记占用与没开始就放弃、交回结局与运行中检查。
//! - [`deliver_in`]：交回结局并释放占用，和一封信件同一个事务；占用已被停止清掉、或会话已删除时不投。唯一出口。
//! - [`post_check_in`]：运行中检查投信（不推送），这个来源已有信在排着、或占用不再 active 时不投。

use std::collections::BTreeMap;

use muliao_platform::db::Tx;
use rusqlite::Connection;

use crate::actor::Waker;
use crate::answers::CallResult;
use crate::directory;
use crate::error::{Result, broken};
use crate::history::since_last_response;
use crate::holds::{self, Hold, HoldOf, HoldStatus};
use crate::mailbox::{NewMail, has_queued_from, post_in, queued_in};
use crate::pending::{HandedOut, Position, position};

/// 发起并发工作时不给 heartbeat_seconds 的默认间隔
pub const DEFAULT_CHECK_SECONDS: f64 = 60.0;

/// 工具参数 heartbeat_seconds → 运行中检查的间隔（run_shell(background)、delegate_task(report_back) 共用）：不给时取默认，
/// 不大于 0（含 NaN）时是参数错误。
pub fn check_interval(heartbeat_seconds: Option<f64>) -> Result<f64, String> {
    let interval = heartbeat_seconds.unwrap_or(DEFAULT_CHECK_SECONDS);
    if interval > 0.0 { Ok(interval) } else { Err("heartbeat_seconds must be greater than 0".to_owned()) }
}

/// 会话现在停在哪（规则在 [`position`]）。没出结果的调用只可能在最后一条回复里，不加载整段上下文。
pub fn position_in(c: &Connection, session_id: &str) -> Result<Position> {
    let ids = [session_id.to_owned()];
    let handed = HandedOut {
        delegated: holds::called_among(c, &ids)?.remove(session_id).unwrap_or_default(),
        approvals: crate::approvals::requested_among(c, &ids)?.remove(session_id).unwrap_or_default(),
    };
    position(&since_last_response(c, session_id)?, &handed, &queued_in(c, session_id)?)
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
    /// 会话已删除
    Gone,
}

/// 交回这项占用的结局并释放它，唯一出口。hold 为 None：会话已删除。会话还停在这项占用对应的挂起调用上、且给了 answer
/// 时作为那次调用的结果；否则作为事件信件（「迟到的回答改为事件」只在这里）。已经释放过的再交回是 bug，当场报错。
pub fn deliver_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    hold: Option<&Hold>,
    source: &Source,
    event_text: &str,
    answer: Option<CallResult>,
) -> Result<Delivered> {
    let Some(hold) = hold else {
        tracing::info!(source = %source.raw(), "outcome for a deleted session dropped");
        return Ok(Delivered::Gone);
    };
    let session_id = hold.session_id.as_str();
    match holds::release_in(tx, hold)? {
        HoldStatus::Cleared => {
            tracing::info!(session_id, hold_id = hold.id, "outcome for a stopped session dropped");
            return Ok(Delivered::Dropped);
        }
        HoldStatus::Released => {
            return Err(broken(format!("hold {} of session {session_id} was already released", hold.id)));
        }
        HoldStatus::Active => {}
    }
    let at = position_in(tx, session_id)?;
    let waiting_on_it = hold.call_id().is_some_and(|c| at.paused().is_some_and(|p| p.get(c).is_some()));
    let delivered = match (answer, hold.call_id()) {
        (Some(answer), Some(call_id)) if waiting_on_it => {
            post_in(
                tx,
                waker,
                session_id,
                NewMail::answer(BTreeMap::from([(call_id.to_owned(), answer)]), false, hold.awaited),
            )?;
            Delivered::Answer
        }
        _ => {
            post_in(tx, waker, session_id, NewMail::event(source, event_text, false, hold.awaited))?;
            Delivered::Event
        }
    };
    tracing::info!(session_id, hold_id = hold.id, delivered_as = ?delivered, "outcome delivered");
    Ok(delivered)
}

/// 运行中检查：给这项工作的会话投一封检查信件（不推送）。这个来源已有信件排着时不投（不堆积）；占用已不是 active 或会话
/// 已删除时也不投。返回是否投了。
pub fn post_check_in(tx: &mut Tx<'_>, waker: &Waker, hold: Option<&Hold>, source: &Source, text: &str) -> Result<bool> {
    let Some(hold) = hold.filter(|h| h.status == HoldStatus::Active) else { return Ok(false) };
    if has_queued_from(tx, &hold.session_id, &source.raw())? {
        return Ok(false);
    }
    post_in(tx, waker, &hold.session_id, NewMail::event(source, text, true, false))?;
    Ok(true)
}

/// 后台任务或到点唤醒在会话上的占用（交回结局与运行中检查用）：会话已删除时 None；会话还在而占用从未登记过是 bug。
pub fn hold_of_in(c: &Connection, session_id: &str, of: &HoldOf) -> Result<Option<Hold>> {
    let hold = holds::get_in(c, session_id, of)?;
    if hold.is_none() && directory::get_in(c, session_id)?.is_some() {
        return Err(broken(format!("session {session_id} has no hold {of:?}")));
    }
    Ok(hold)
}

/// 事件信件的来源：交给模型时套在事件外面，界面据此显示（[`EventSource`]）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Source {
    /// 后台任务
    Job(i64),
    /// thread（会话 id）
    Thread(String),
    /// 到点唤醒
    Wake,
}

impl Source {
    pub fn raw(&self) -> String {
        match self {
            Source::Job(id) => format!("job:{id}"),
            Source::Thread(id) => format!("thread:{id}"),
            Source::Wake => crate::wakeups::WAKE_ME.to_owned(),
        }
    }
}

/// 事件来源解析成的种类（界面用，不自己解析字符串）。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EventSource {
    Thread { raw: String, session_id: String },
    Job { raw: String, job_id: i64 },
    Other { raw: String },
}

impl EventSource {
    /// 来源原文（如 `job:7`、`thread:<id>`）。
    pub fn raw(&self) -> &str {
        match self {
            EventSource::Thread { raw, .. } | EventSource::Job { raw, .. } | EventSource::Other { raw } => raw,
        }
    }
}

pub fn parse_source(raw: &str) -> EventSource {
    if let Some(id) = raw.strip_prefix("thread:").filter(|s| !s.is_empty()) {
        return EventSource::Thread { raw: raw.to_owned(), session_id: id.to_owned() };
    }
    let job_id = raw.strip_prefix("job:").filter(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()));
    if let Some(job_id) = job_id.and_then(|s| s.parse().ok()) {
        return EventSource::Job { raw: raw.to_owned(), job_id };
    }
    EventSource::Other { raw: raw.to_owned() }
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

    /// 登记一项进行中的后台任务：会话据此知道工作还没做完。awaited：用户是否在等它的结局。
    pub fn hold_in(&self, tx: &mut Tx<'_>, session_id: &str, job_id: i64, awaited: bool) -> Result<()> {
        holds::hold_in(tx, session_id, HoldOf::Job { job_id }, awaited)?;
        Ok(())
    }

    /// 这项工作没开始就放弃了：清掉占用，不交回结局。只在发起它的那次运行里调用（停止排在运行退出之后、删除拒绝忙会话），
    /// 所以占用一定还 active。
    pub fn abandoned_in(&self, tx: &mut Tx<'_>, session_id: &str, job_id: i64) -> Result<()> {
        let hold = hold_of_in(tx, session_id, &HoldOf::Job { job_id })?.ok_or_else(|| {
            broken(format!("session {session_id} was deleted while its run was starting job {job_id}"))
        })?;
        holds::abandon_in(tx, &hold)
    }

    /// 后台任务结束了：往会话的信箱投一封事件信件，同时释放占用。
    pub fn ended_in(&self, tx: &mut Tx<'_>, session_id: &str, job_id: i64, text: &str) -> Result<Delivered> {
        let hold = hold_of_in(tx, session_id, &HoldOf::Job { job_id })?;
        deliver_in(tx, &self.waker, hold.as_ref(), &Source::Job(job_id), text, None)
    }

    /// 后台任务还在进行：给会话投一封检查信件。
    pub fn checked_in(&self, tx: &mut Tx<'_>, session_id: &str, job_id: i64, text: &str) -> Result<bool> {
        let hold = hold_of_in(tx, session_id, &HoldOf::Job { job_id })?;
        post_check_in(tx, &self.waker, hold.as_ref(), &Source::Job(job_id), text)
    }
}

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
