//! 热历史（会话领域的读模型，docs/architecture.md 第 22 节）：一段时间里 agent 的会话做了什么、用户说了什么，以及一个会话
//! 的时间线。给热历史的两个工具用（history 领域的 activity_overview、session_read）；冷历史是 agent 自己写的工作历史
//! （history 领域）。
//!
//! 「用户说了什么」只按信件记下的发送方来源认（[`Mail::origin`] 为 user：用户在界面上发的消息与对提问的回答），不从文字
//! 猜谁发的；时间线里的用户消息条目按交出信件时记在请求上的发送方认（没记的算会话的发起方）。时间线的条目与会话页同一份
//! 投影（[`to_items`]），只要文字，不用工具的展示规则。

use muliao_platform::ids::{AgentId, RunId, SessionId};
use std::collections::BTreeSet;

use chrono::{DateTime, Utc};
use rusqlite::Connection;

use crate::activity::{WorkState, work_states_in};
use crate::answers::{AskResult, CallResult, answer_lines};
use crate::directory::{Session, among_in, with_prefix_in};
use crate::history::{Span, spans_between_in, transcript_in};
use crate::mailbox::{Mail, MailBody, first_arrivals_between_in, sent_by_in};
use crate::origins::Origin;
use crate::runs::started_between_in;
use crate::transcript::{Item, Renders, UserItem, to_items_with};
use muliao_platform::errors::{Result, invalid, not_found};

/// 一次最终失败的运行：开始的时刻与错误说明。
#[derive(Debug, Clone, PartialEq)]
pub struct RunFailure {
    pub run_id: RunId,
    pub at: DateTime<Utc>,
    pub error: String,
}

/// 一段时间里有活动（保存了消息或收到了信件）的一个会话。
#[derive(Debug, Clone, PartialEq)]
pub struct DaySession {
    pub session: Session,
    /// 此刻的工作状态（与会话列表同一条规则）
    pub state: WorkState,
    /// 这段时间里保存的消息；只收到了信件、还没保存消息的为 None
    pub span: Option<Span>,
    /// 这段时间里第一次活动（第一封信件到达或第一条消息保存）
    pub first_at: DateTime<Utc>,
    /// 这段时间里开始的运行数，与其中最终失败的
    pub runs: usize,
    pub failures: Vec<RunFailure>,
}

/// 用户在界面上说的一段话：发的消息（content），或对提问的回答（每题一行）。
#[derive(Debug, Clone, PartialEq)]
pub struct UserWords {
    pub session_id: SessionId,
    pub at: DateTime<Utc>,
    pub text: String,
    /// 随消息发送的文件数
    pub attachments: usize,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Activity {
    /// 按第一次活动排序
    pub sessions: Vec<DaySession>,
    /// 按时间排序
    pub words: Vec<UserWords>,
}

/// [start, end) 里这些 agent（None 为全部）有活动的会话，以及用户这段时间在其中说的话。
pub fn between_in(
    c: &Connection,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
    agent_ids: Option<&[AgentId]>,
) -> Result<Activity> {
    let spans = spans_between_in(c, start, end)?;
    let arrivals = first_arrivals_between_in(c, start, end)?;
    let active: BTreeSet<SessionId> = spans.keys().chain(arrivals.keys()).cloned().collect();
    let rows = among_in(c, &active.into_iter().collect::<Vec<_>>())?;
    let sessions: Vec<Session> =
        rows.into_values().filter(|s| agent_ids.is_none_or(|ids| ids.contains(&s.agent_id))).collect();
    let ids: Vec<SessionId> = sessions.iter().map(|s| s.id.clone()).collect();
    let states = work_states_in(c, &ids)?;
    let mut runs = started_between_in(c, start, end)?;
    let words = sent_by_in(c, &ids, Origin::User, Some(start), Some(end))?;
    let mut found: Vec<DaySession> = sessions
        .into_iter()
        .map(|session| {
            let span = spans.get(&session.id).cloned();
            let first_at = [arrivals.get(&session.id).copied(), span.as_ref().map(|s| s.first_at)]
                .into_iter()
                .flatten()
                .min()
                .expect("an active session has mail or messages in the range");
            let runs = runs.remove(&session.id).unwrap_or_default();
            // 按运行开始的时刻归到哪天（Python 版按排队的时刻；Rust 版没有任务队列，只有跨午夜才开始的运行会归到不同的天）
            let failures = runs
                .iter()
                .filter_map(|r| {
                    let error = r.failure()?.to_owned();
                    Some(RunFailure { run_id: r.id, at: r.started_at, error })
                })
                .collect();
            Ok(DaySession { state: states[&session.id], span, first_at, runs: runs.len(), failures, session })
        })
        .collect::<Result<_>>()?;
    found.sort_by(|a, b| (a.first_at, &a.session.id).cmp(&(b.first_at, &b.session.id)));
    Ok(Activity { sessions: found, words: words.iter().map(words_of).collect() })
}

fn words_of(mail: &Mail) -> UserWords {
    let text = if let MailBody::Answer { result, .. } = &mail.body {
        let asked: Vec<&AskResult> = match result {
            CallResult::AskUser(a) => vec![a],
            CallResult::AskAgent(_) | CallResult::Approval(_) => Vec::new(),
        };
        let mut lines = vec!["(answers to ask_user)".to_owned()];
        lines.extend(answer_lines(&asked));
        lines.join("\n")
    } else {
        mail.body.content().to_owned()
    };
    let attachments = mail.body.attachments().len();
    UserWords { session_id: mail.session_id.clone(), at: mail.created_at, text, attachments }
}

/// 时间线里的用户消息条目与它的发送方来源：交出信件时记下的发送方；没记的（迟到的回答等）算会话的发起方。
#[derive(Debug, Clone, PartialEq)]
pub struct SentUser {
    pub user: UserItem,
    pub sender: Origin,
}

impl AsRef<UserItem> for SentUser {
    fn as_ref(&self) -> &UserItem {
        &self.user
    }
}

/// 时间线的一条：所在消息保存的时刻与展示条目。
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    pub at: DateTime<Utc>,
    pub item: Item<SentUser>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Timeline {
    pub session: Session,
    pub entries: Vec<Entry>,
}

/// 一个会话（id 或唯一的 id 前缀）的时间线；since 只取这之后保存的消息。
pub fn timeline_in(c: &Connection, session: &str, since: Option<DateTime<Utc>>) -> Result<Timeline> {
    let mut matches = with_prefix_in(c, session, 2)?;
    let row = match matches.len() {
        0 => return Err(not_found(format!("no session id starts with {session:?}"))),
        1 => matches.remove(0),
        _ => {
            return Err(invalid(format!(
                "session id prefix {session:?} matches several sessions; give more characters"
            )));
        }
    };
    let stored = transcript_in(c, &row.id, since)?;
    let renders = Renders::new();
    let starter = row.origin.origin();
    let mut entries = Vec::new();
    for s in &stored {
        let items = to_items_with([&s.message], &renders, Some(s.created_at), |user, sender| SentUser {
            user,
            sender: sender.unwrap_or(starter),
        })?;
        entries.extend(items.into_iter().map(|item| Entry { at: s.created_at, item }));
    }
    Ok(Timeline { session: row, entries })
}
