//! 会话信箱（`mail` 表的唯一读写处）：一切要交给 agent 的东西都是往会话信箱里投一封信件，会话 actor 按到达顺序把信件
//! 交给模型。设计见 docs/architecture.md「会话信箱」。
//!
//! - 投信（[`post_in`]）：用户消息、对挂起调用的回答、触发器与委派来的任务、后台任务与 thread 的事件、运行中检查。提交后
//!   叫醒会话 actor，要不要跑由它按唤醒规则（[`crate::actor`]）判断。
//! - 一封信交给了哪次运行记在信上（`taken_by`，写处只有 [`give_in`] 与 [`consume_in`]）：运行开头取走的、交给模型的那一步
//!   保存时（这时同时记为已处理）。
//! - 用户停止会话、运行最终失败时没交给模型的信件记为被丢弃（[`discard_in`]），留着记录。

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, Row, params};

use crate::actor::Waker;
use crate::answers::CallResult;
use crate::origins::Origin;
use crate::work::EventSource;
use muliao_platform::db::Tx;
use muliao_platform::errors::{Result, broken, not_found};

/// 信件的种类与它自己的内容。
#[derive(Debug, Clone, PartialEq)]
pub enum MailBody {
    /// 交给模型的原文：用户在界面发的消息（可带附件）、触发器事件、另一个 agent 委派来的任务。origin 是经来信入口投进来的
    /// 发送方来源，「用户说了什么」只按它认；delegation_id：委派来的信件是哪项委派（thread_calls 的行）
    Message { origin: Origin, content: String, attachments: Vec<String>, delegation_id: Option<i64> },
    /// 对挂起调用的回答（调用 id -> 结果）：作为那次调用的结果交回，不是一段新消息。from_user：用户的回答（否则是交回的
    /// 结局）
    Answer { answers: BTreeMap<String, CallResult>, from_user: bool },
    /// 后台任务、交回的结果、运行中检查（check_in）：套上来源（如 job:12、thread:<会话 id>）交给模型，界面单独显示
    Event { source: EventSource, content: String, check_in: bool },
}

impl MailBody {
    fn kind(&self) -> &'static str {
        match self {
            MailBody::Message { .. } => "message",
            MailBody::Answer { .. } => "answer",
            MailBody::Event { .. } => "event",
        }
    }

    /// 发送方来源（存在 origin 列，热历史按它认「用户说了什么」）：消息的发送方，用户的回答是 user，事件与交回的结局没有。
    pub fn origin(&self) -> Option<Origin> {
        match self {
            MailBody::Message { origin, .. } => Some(*origin),
            MailBody::Answer { from_user, .. } => from_user.then_some(Origin::User),
            MailBody::Event { .. } => None,
        }
    }

    /// 文字内容；回答没有。
    pub fn content(&self) -> &str {
        match self {
            MailBody::Message { content, .. } | MailBody::Event { content, .. } => content,
            MailBody::Answer { .. } => "",
        }
    }

    /// 消息的附件（文件存储 id）。
    pub fn attachments(&self) -> &[String] {
        match self {
            MailBody::Message { attachments, .. } => attachments,
            _ => &[],
        }
    }

    /// 回答的调用 id -> 结果；不是回答的为空。
    pub fn answers(&self) -> Option<&BTreeMap<String, CallResult>> {
        match self {
            MailBody::Answer { answers, .. } => Some(answers),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum MailState {
    Queued,
    /// 交给模型的那一步已经保存
    Consumed,
    /// 没交给模型就不再处理了（用户停止、运行最终失败），留着记录
    Discarded,
}

impl MailState {
    fn as_str(self) -> &'static str {
        match self {
            MailState::Queued => "queued",
            MailState::Consumed => "consumed",
            MailState::Discarded => "discarded",
        }
    }

    fn parse(s: &str) -> Result<Self> {
        Ok(match s {
            "queued" => MailState::Queued,
            "consumed" => MailState::Consumed,
            "discarded" => MailState::Discarded,
            _ => return Err(broken(format!("unknown mail state {s:?}"))),
        })
    }
}

/// 信箱里的一封信。
#[derive(Debug, Clone, PartialEq)]
pub struct Mail {
    pub id: i64,
    pub session_id: String,
    pub body: MailBody,
    /// 用户在等它的回答
    pub awaited: bool,
    pub state: MailState,
    pub taken_by: Option<i64>,
    pub created_at: DateTime<Utc>,
}

/// 一封要投的信（[`post_in`] 的输入）。
#[derive(Debug, Clone)]
pub struct NewMail {
    pub body: MailBody,
    pub awaited: bool,
}

impl NewMail {
    /// 来信入口投的原文消息。
    pub fn message(origin: Origin, content: impl Into<String>, awaited: bool) -> Self {
        let body = MailBody::Message { origin, content: content.into(), attachments: Vec::new(), delegation_id: None };
        NewMail { body, awaited }
    }

    /// 事件（后台任务、交回的结果、到点唤醒）；check_in：运行中检查。
    pub fn event(source: EventSource, content: impl Into<String>, check_in: bool, awaited: bool) -> Self {
        NewMail { body: MailBody::Event { source, content: content.into(), check_in }, awaited }
    }

    /// 对挂起调用的回答。from_user：用户的回答（否则是交回的结局）。
    pub fn answer(answers: BTreeMap<String, CallResult>, from_user: bool, awaited: bool) -> Self {
        NewMail { body: MailBody::Answer { answers, from_user }, awaited }
    }
}

const COLUMNS: &str = "id, session_id, kind, source, origin, content, check_in, attachments, answers, awaited, \
     delegation_id, state, taken_by, created_at";

/// 一行的原样：文本列在 [`RawMail::parse`] 里解析（解析不了是数据坏了，不是 SQL 错误）。
struct RawMail {
    id: i64,
    session_id: String,
    kind: String,
    source: Option<String>,
    origin: Option<String>,
    content: String,
    check_in: bool,
    attachments: String,
    answers: String,
    awaited: bool,
    delegation_id: Option<i64>,
    state: String,
    taken_by: Option<i64>,
    created_at: DateTime<Utc>,
}

impl RawMail {
    fn of(r: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(RawMail {
            id: r.get(0)?,
            session_id: r.get(1)?,
            kind: r.get(2)?,
            source: r.get(3)?,
            origin: r.get(4)?,
            content: r.get(5)?,
            check_in: r.get(6)?,
            attachments: r.get(7)?,
            answers: r.get(8)?,
            awaited: r.get(9)?,
            delegation_id: r.get(10)?,
            state: r.get(11)?,
            taken_by: r.get(12)?,
            created_at: r.get(13)?,
        })
    }

    fn parse(self) -> Result<Mail> {
        let id = self.id;
        let bad = |what: &str, e: serde_json::Error| broken(format!("{what} of mail {id}: {e}"));
        let origin = self.origin.as_deref().map(Origin::parse).transpose()?;
        let body = match (self.kind.as_str(), origin, self.source) {
            ("message", Some(origin), None) => MailBody::Message {
                origin,
                content: self.content,
                attachments: serde_json::from_str(&self.attachments).map_err(|e| bad("attachments", e))?,
                delegation_id: self.delegation_id,
            },
            ("answer", None | Some(Origin::User), None) => MailBody::Answer {
                answers: serde_json::from_str(&self.answers).map_err(|e| bad("answers", e))?,
                from_user: origin.is_some(),
            },
            ("event", None, Some(raw)) => match EventSource::parse(&raw) {
                Some(source) => MailBody::Event { source, content: self.content, check_in: self.check_in },
                None => return Err(broken(format!("event mail {id} has an unknown source {raw:?}"))),
            },
            (kind, origin, source) => {
                return Err(broken(format!("mail {id} of kind {kind:?} has origin {origin:?} and source {source:?}")));
            }
        };
        Ok(Mail {
            id,
            session_id: self.session_id,
            body,
            awaited: self.awaited,
            state: MailState::parse(&self.state)?,
            taken_by: self.taken_by,
            created_at: self.created_at,
        })
    }
}

fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Mail>> {
    let sql = format!("SELECT {COLUMNS} FROM mail WHERE {filter} ORDER BY id");
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt.query_map(params, RawMail::of)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(RawMail::parse).collect()
}

/// 在调用方事务里往会话信箱投一封信，提交后叫醒会话 actor。
pub fn post_in(tx: &mut Tx<'_>, waker: &Waker, session_id: &str, mail: NewMail) -> Result<i64> {
    let exists: Option<i64> =
        tx.query_row("SELECT 1 FROM sessions WHERE id = ?1", [session_id], |r| r.get(0)).optional()?;
    if exists.is_none() {
        return Err(not_found(format!("session {session_id:?} not found")));
    }
    let body = &mail.body;
    let (source, check_in, delegation, answers) = match body {
        MailBody::Message { delegation_id, .. } => (None, false, *delegation_id, None),
        MailBody::Answer { answers, .. } => (None, false, None, Some(answers)),
        MailBody::Event { source, check_in, .. } => (Some(source.raw()), *check_in, None, None),
    };
    tx.execute(
        "INSERT INTO mail (session_id, kind, source, origin, content, check_in, attachments, answers, awaited, \
         delegation_id, state, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 'queued', ?11)",
        params![
            session_id,
            body.kind(),
            source,
            body.origin().map(Origin::as_str),
            body.content(),
            check_in,
            serde_json::to_string(body.attachments()).expect("ids serialize"),
            serde_json::to_string(&answers.cloned().unwrap_or_default()).expect("answers serialize"),
            mail.awaited,
            delegation,
            Utc::now(),
        ],
    )?;
    let id = tx.last_insert_rowid();
    tracing::info!(session_id, mail_id = id, kind = body.kind(), source = source.as_deref(), "mail posted");
    waker.wake_on_commit(tx, session_id);
    Ok(id)
}

/// 会话里还在排队的信件，按到达顺序。
pub fn queued_in(c: &Connection, session_id: &str) -> Result<Vec<Mail>> {
    query(c, "session_id = ?1 AND state = 'queued'", [session_id])
}

/// 这些会话里还在排队的信件，各自按到达顺序（一次查询）；每个会话都在结果里。
pub fn queued_among(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, Vec<Mail>>> {
    let mut out: BTreeMap<String, Vec<Mail>> = session_ids.iter().map(|s| (s.clone(), Vec::new())).collect();
    if session_ids.is_empty() {
        return Ok(out);
    }
    for m in query(
        c,
        &format!("state = 'queued' AND session_id IN ({})", placeholders(session_ids.len())),
        rusqlite::params_from_iter(session_ids),
    )? {
        out.entry(m.session_id.clone()).or_default().push(m);
    }
    Ok(out)
}

pub(crate) fn placeholders(n: usize) -> String {
    vec!["?"; n].join(", ")
}

/// 这些排着的信件交给这次运行（运行开头取走时）。已经不在排队的不动。
pub fn give_in(c: &Connection, ids: &[i64], run_id: i64) -> Result<()> {
    for id in ids {
        c.execute("UPDATE mail SET taken_by = ?1 WHERE id = ?2 AND state = 'queued'", params![run_id, id])?;
    }
    Ok(())
}

/// 交给模型的那一步保存时记下处理掉的信件。同一会话只有一个 actor，取走的信不可能被别人处理掉。
pub fn consume_in(c: &Connection, ids: &[i64], run_id: i64) -> Result<()> {
    for id in ids {
        let n = c.execute(
            "UPDATE mail SET state = 'consumed', taken_by = ?1 WHERE id = ?2 AND state = 'queued'",
            params![run_id, id],
        )?;
        if n != 1 {
            return Err(broken(format!("run {run_id} handed mail {id} to the model but it was no longer queued")));
        }
    }
    Ok(())
}

/// 这些还没交给模型的信件记为被丢弃。返回丢弃的信件 id。
pub fn discard_in(c: &Connection, session_id: &str, ids: &[i64]) -> Result<Vec<i64>> {
    for id in ids {
        c.execute("UPDATE mail SET state = 'discarded' WHERE id = ?1", [id])?;
    }
    if !ids.is_empty() {
        tracing::info!(session_id, discarded = ?ids, "mailbox cleared");
    }
    Ok(ids.to_vec())
}

/// 这次运行处理掉的信件（交给了它、已交给模型）。
pub fn consumed_by_in(c: &Connection, session_id: &str, run_id: i64) -> Result<Vec<Mail>> {
    query(c, "session_id = ?1 AND taken_by = ?2 AND state = 'consumed'", params![session_id, run_id])
}

/// 会话里这些状态的委派来的信件，按到达顺序。
pub fn delegated_mail_in(c: &Connection, session_id: &str, states: &[MailState]) -> Result<Vec<Mail>> {
    let states: Vec<String> = states.iter().map(|s| format!("'{}'", s.as_str())).collect();
    query(
        c,
        &format!("session_id = ?1 AND delegation_id IS NOT NULL AND state IN ({})", states.join(", ")),
        [session_id],
    )
}

/// 界面上显示在历史之后的信件：排队中的，以及最近一次停止丢弃的（之后再有信件交给模型就不再显示）。
pub fn visible_in(c: &Connection, session_id: &str) -> Result<Vec<Mail>> {
    let last_consumed: Option<i64> =
        c.query_row("SELECT max(id) FROM mail WHERE session_id = ?1 AND state = 'consumed'", [session_id], |r| {
            r.get(0)
        })?;
    query(
        c,
        "session_id = ?1 AND (state = 'queued' OR (state = 'discarded' AND id > ?2))",
        params![session_id, last_consumed.unwrap_or(0)],
    )
}

/// 这个发送方是否已有信件在排队（运行中检查据此不堆积）。
pub fn has_queued_from(c: &Connection, session_id: &str, source: &str) -> Result<bool> {
    Ok(c.query_row(
        "SELECT EXISTS (SELECT 1 FROM mail WHERE session_id = ?1 AND source = ?2 AND state = 'queued')",
        params![session_id, source],
        |r| r.get(0),
    )?)
}

/// 这些会话里这种来源的发送方投的信件（`origin`），按到达顺序；给了时间只取 [start, end) 里到的。热历史按它认「用户说了
/// 什么」。
pub fn sent_by_in(
    c: &Connection,
    session_ids: &[String],
    origin: Origin,
    start: Option<DateTime<Utc>>,
    end: Option<DateTime<Utc>>,
) -> Result<Vec<Mail>> {
    if session_ids.is_empty() {
        return Ok(Vec::new());
    }
    let n = session_ids.len();
    let filter = format!(
        "session_id IN ({}) AND origin = ?{} AND (?{} IS NULL OR created_at >= ?{}) AND (?{} IS NULL OR created_at < ?{})",
        placeholders(n),
        n + 1,
        n + 2,
        n + 2,
        n + 3,
        n + 3
    );
    let mut values: Vec<&dyn rusqlite::ToSql> = session_ids.iter().map(|s| s as &dyn rusqlite::ToSql).collect();
    let origin = origin.as_str();
    values.extend([&origin as &dyn rusqlite::ToSql, &start, &end]);
    query(c, &filter, rusqlite::params_from_iter(values))
}

/// 在 [start, end) 里收到过信件的会话 -> 这段时间里第一封到达的时刻。
pub fn first_arrivals_between_in(
    c: &Connection,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Result<BTreeMap<String, DateTime<Utc>>> {
    let mut stmt = c.prepare(
        "SELECT session_id, min(created_at) FROM mail WHERE created_at >= ?1 AND created_at < ?2 GROUP BY session_id",
    )?;
    let rows = stmt.query_map(params![start, end], |r| Ok((r.get(0)?, r.get(1)?)))?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}
