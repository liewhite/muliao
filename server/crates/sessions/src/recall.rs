//! 热历史（会话领域的读模型，docs/architecture.md 第 22 节）：一段时间里 agent 的会话做了什么、用户说了什么，以及一个会话
//! 的时间线。给热历史的两个工具用（history 领域的 activity_overview、session_read）；冷历史是 agent 自己写的工作历史
//! （history 领域）。
//!
//! 「用户说了什么」只按信件记下的发送方来源认（[`Mail::origin`] 为 user：用户在界面上发的消息与对提问的回答），不从文字
//! 猜谁发的；时间线里的用户消息条目按保存它的那次运行处理的用户信件整段对上（[`from_user`]）。时间线的条目与会话页同一份
//! 投影（[`to_items`]），只要文字，不用工具的展示规则。

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Utc};
use rusqlite::Connection;

use crate::activity::{WorkState, work_states_in};
use crate::answers::{AskResult, CallResult, answer_lines};
use crate::directory::{Session, among_in, with_prefix_in};
use crate::error::{Result, broken, invalid, not_found};
use crate::history::{Span, spans_between_in, transcript_in};
use crate::mailbox::{Mail, MailKind, MailState, first_arrivals_between_in, sent_by_in};
use crate::origins::Origin;
use crate::runs::started_between_in;
use crate::transcript::{Item, Renders, UserItem, to_items};

/// 一次最终失败的运行：开始的时刻与错误说明。
#[derive(Debug, Clone, PartialEq)]
pub struct RunFailure {
    pub run_id: i64,
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
    pub session_id: String,
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
    agent_ids: Option<&[String]>,
) -> Result<Activity> {
    let spans = spans_between_in(c, start, end)?;
    let arrivals = first_arrivals_between_in(c, start, end)?;
    let active: BTreeSet<String> = spans.keys().chain(arrivals.keys()).cloned().collect();
    let rows = among_in(c, &active.into_iter().collect::<Vec<_>>())?;
    let sessions: Vec<Session> =
        rows.into_values().filter(|s| agent_ids.is_none_or(|ids| ids.contains(&s.agent_id))).collect();
    let ids: Vec<String> = sessions.iter().map(|s| s.id.clone()).collect();
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
                .filter(|r| r.failed())
                .map(|r| {
                    let error = r.failure().ok_or_else(|| broken(format!("run {} failed without an error", r.id)))?;
                    Ok(RunFailure { run_id: r.id, at: r.started_at, error: error.to_owned() })
                })
                .collect::<Result<_>>()?;
            Ok(DaySession { state: states[&session.id], span, first_at, runs: runs.len(), failures, session })
        })
        .collect::<Result<_>>()?;
    found.sort_by(|a, b| (a.first_at, &a.session.id).cmp(&(b.first_at, &b.session.id)));
    Ok(Activity { sessions: found, words: words.iter().map(words_of).collect() })
}

fn words_of(mail: &Mail) -> UserWords {
    let text = if mail.kind == MailKind::Answer {
        let asked: Vec<&AskResult> = mail
            .answers
            .values()
            .filter_map(|r| match r {
                CallResult::AskUser(a) => Some(a),
                _ => None,
            })
            .collect();
        let mut lines = vec!["(answers to ask_user)".to_owned()];
        lines.extend(answer_lines(&asked));
        lines.join("\n")
    } else {
        mail.content.clone()
    };
    UserWords { session_id: mail.session_id.clone(), at: mail.created_at, text, attachments: mail.attachments.len() }
}

/// 时间线里的用户消息条目与它的发送方来源：用户在界面上写的是 user，否则是会话的发起方（别的来源只续写自己发起的会话）。
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
    // 每次运行交给模型的用户消息（信件交给模型时原文作为一整段，content.rs）
    let mut typed: BTreeMap<i64, Vec<Mail>> = BTreeMap::new();
    for m in sent_by_in(c, std::slice::from_ref(&row.id), Origin::User, None, None)? {
        if let (MailKind::Message, MailState::Consumed, Some(run)) = (m.kind, m.state, m.taken_by) {
            typed.entry(run).or_default().push(m);
        }
    }
    let renders = Renders::new();
    let mut entries = Vec::new();
    for s in &stored {
        for item in to_items([&s.message], &renders, Some(s.created_at))? {
            let typed = typed.get(&s.run_id).map(Vec::as_slice).unwrap_or_default();
            let item = item.map_user(|user| {
                let sender = if from_user(&user, typed) { Origin::User } else { row.origin.origin() };
                SentUser { user, sender }
            });
            entries.push(Entry { at: s.created_at, item });
        }
    }
    Ok(Timeline { session: row, entries })
}

/// 用户消息条目里有用户在界面上写的一封信：typed 是保存这条消息的那次运行处理的用户消息；只有附件的按条目带不带附件认。
fn from_user(item: &UserItem, typed: &[Mail]) -> bool {
    typed.iter().any(|m| {
        if m.content.trim().is_empty() { !item.attachments.is_empty() } else { has_segment(&item.text, &m.content) }
    })
}

/// 条目文字里有这一整段：一条用户消息条目是连续内容段用换行连起来的（transcript 的 UserContent）。
fn has_segment(text: &str, segment: &str) -> bool {
    text == segment
        || text.starts_with(&format!("{segment}\n"))
        || text.ends_with(&format!("\n{segment}"))
        || text.contains(&format!("\n{segment}\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_segment_is_a_whole_line_run() {
        assert!(has_segment("hi", "hi"));
        assert!(has_segment("hi\nthere", "hi"));
        assert!(has_segment("a\nhi", "hi"));
        assert!(has_segment("a\nhi\nb", "hi"));
        assert!(!has_segment("high", "hi"));
        assert!(!has_segment("a\nhigh", "hi"));
    }
}
