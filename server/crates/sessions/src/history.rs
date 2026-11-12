//! 会话历史（`messages`、`message_texts`、`compactions` 的唯一读写处）：发给模型的上下文是最新摘要 + 压缩切点之后的原始
//! 消息。原始消息不删除，压缩只决定从哪里开始发。

use std::collections::BTreeMap;

use agenter::{Message, Request, RequestPart};
use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, params};

use crate::conventions::{CONTINUATION, answer_text, mark_stopped, measured_responses, searchable_text};
use crate::error::{Result, broken};
use crate::mailbox::placeholders;

/// 一条存下的消息。
#[derive(Debug, Clone, PartialEq)]
pub struct Stored {
    pub id: i64,
    pub message: Message,
    pub created_at: DateTime<Utc>,
    /// 保存它的运行
    pub run_id: i64,
}

fn decode(id: i64, payload: &str) -> Result<Message> {
    serde_json::from_str(payload).map_err(|e| broken(format!("message {id} does not parse: {e}")))
}

fn stored(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Stored>> {
    let sql = format!("SELECT id, payload, created_at, run_id FROM messages WHERE {filter}");
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt
        .query_map(params, |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get(2)?, r.get(3)?)))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter()
        .map(|(id, payload, created_at, run_id)| Ok(Stored { id, message: decode(id, &payload)?, created_at, run_id }))
        .collect()
}

/// 把这些消息接在会话历史末尾（run_id：保存它们的运行），同一事务写可搜索文字。返回写下的消息 id。
pub fn append_in(c: &Connection, session_id: &str, run_id: i64, messages: &[Message]) -> Result<Vec<i64>> {
    let now = Utc::now();
    let mut ids = Vec::new();
    for m in messages {
        c.execute(
            "INSERT INTO messages (session_id, run_id, payload, created_at) VALUES (?1, ?2, ?3, ?4)",
            params![session_id, run_id, serde_json::to_string(m).expect("messages serialize"), now],
        )?;
        let id = c.last_insert_rowid();
        let text = searchable_text(m);
        if !text.is_empty() {
            c.execute("INSERT INTO message_texts (message_id, text) VALUES (?1, ?2)", params![id, text])?;
        }
        ids.push(id);
    }
    Ok(ids)
}

/// 给历史最后一条消息标上停止。
pub fn mark_last_stopped_in(c: &Connection, session_id: &str) -> Result<()> {
    let last = stored(c, "session_id = ?1 ORDER BY id DESC LIMIT 1", [session_id])?
        .pop()
        .ok_or_else(|| broken(format!("session {session_id} has history but no message rows")))?;
    let mut message = last.message;
    mark_stopped(&mut message);
    c.execute(
        "UPDATE messages SET payload = ?1 WHERE id = ?2",
        params![serde_json::to_string(&message).expect("messages serialize"), last.id],
    )?;
    Ok(())
}

pub fn last_message_id_in(c: &Connection, session_id: &str) -> Result<Option<i64>> {
    Ok(c.query_row("SELECT max(id) FROM messages WHERE session_id = ?1", [session_id], |r| r.get(0))?)
}

/// 保存历史最后一条消息的运行（续跑接管时看停下的部分是谁保存的）。
pub fn last_saved_by_in(c: &Connection, session_id: &str) -> Result<Option<i64>> {
    Ok(c.query_row("SELECT run_id FROM messages WHERE session_id = ?1 ORDER BY id DESC LIMIT 1", [session_id], |r| {
        r.get(0)
    })
    .optional()?)
}

/// 这次运行（含之前几次尝试）保存的消息，按顺序。
pub fn saved_by_in(c: &Connection, session_id: &str, run_id: i64) -> Result<Vec<Stored>> {
    stored(c, "session_id = ?1 AND run_id = ?2 ORDER BY id", params![session_id, run_id])
}

/// 完整原始历史（不受压缩影响）；since 只取这之后保存的。
pub fn transcript_in(c: &Connection, session_id: &str, since: Option<DateTime<Utc>>) -> Result<Vec<Stored>> {
    stored(c, "session_id = ?1 AND (?2 IS NULL OR created_at >= ?2) ORDER BY id", params![session_id, since])
}

/// id 大于 after 的消息，按顺序。
pub fn rows_after(c: &Connection, session_id: &str, after: i64) -> Result<Vec<Stored>> {
    stored(c, "session_id = ?1 AND id > ?2 ORDER BY id", params![session_id, after])
}

/// 从最后一条模型回复起的消息（还没有回复时是全部）。之前每条回复的调用在下一条回复之前都已有结果，所以还没有结果的
/// 调用只可能在这一段里：查它们不必加载整段上下文。
pub fn since_last_response(c: &Connection, session_id: &str) -> Result<Vec<Message>> {
    let mut tail: Vec<Message> = Vec::new();
    let mut before = i64::MAX;
    loop {
        // 分批往回读：通常一两条就到最后一条回复
        let rows = stored(c, "session_id = ?1 AND id < ?2 ORDER BY id DESC LIMIT 4", params![session_id, before])?;
        let n = rows.len();
        for row in rows {
            let is_response = matches!(row.message, Message::Assistant(_));
            before = row.id;
            tail.push(row.message);
            if is_response {
                tail.reverse();
                return Ok(tail);
            }
        }
        if n < 4 {
            tail.reverse();
            return Ok(tail);
        }
    }
}

/// since_last_response 的批量版本：先一次取各会话的最后一条，多数停在一条回复上就是它；停在请求上的再逐个往回读。
pub fn since_last_response_among(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, Vec<Message>>> {
    let mut tails: BTreeMap<String, Vec<Message>> = session_ids.iter().map(|s| (s.clone(), Vec::new())).collect();
    if session_ids.is_empty() {
        return Ok(tails);
    }
    let sql = format!(
        "SELECT session_id, id, payload FROM messages WHERE id IN \
         (SELECT max(id) FROM messages WHERE session_id IN ({}) GROUP BY session_id)",
        placeholders(session_ids.len())
    );
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(session_ids), |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for (session_id, id, payload) in rows {
        let message = decode(id, &payload)?;
        let tail =
            if matches!(message, Message::Assistant(_)) { vec![message] } else { since_last_response(c, &session_id)? };
        tails.insert(session_id, tail);
    }
    Ok(tails)
}

/// 会话最近的回答：最后一条回复的文字；还没有回复时为空。
pub fn latest_answer(c: &Connection, session_id: &str) -> Result<String> {
    Ok(answer_of(&since_last_response(c, session_id)?))
}

pub fn latest_answers_among(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, String>> {
    Ok(since_last_response_among(c, session_ids)?.into_iter().map(|(k, t)| (k, answer_of(&t))).collect())
}

fn answer_of(tail: &[Message]) -> String {
    match tail.first() {
        Some(Message::Assistant(r)) => answer_text(r),
        _ => String::new(),
    }
}

/// 一段时间里保存过消息的会话：条数与第一条、最后一条的保存时刻。
#[derive(Debug, Clone, PartialEq)]
pub struct Span {
    pub messages: i64,
    pub first_at: DateTime<Utc>,
    pub last_at: DateTime<Utc>,
}

pub fn spans_between_in(c: &Connection, start: DateTime<Utc>, end: DateTime<Utc>) -> Result<BTreeMap<String, Span>> {
    let mut stmt = c.prepare(
        "SELECT session_id, count(*), min(created_at), max(created_at) FROM messages \
         WHERE created_at >= ?1 AND created_at < ?2 GROUP BY session_id",
    )?;
    let rows = stmt.query_map(params![start, end], |r| {
        Ok((r.get::<_, String>(0)?, Span { messages: r.get(1)?, first_at: r.get(2)?, last_at: r.get(3)? }))
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

/// 可搜索文字里提到关键字的会话的 SQL 子查询（一个参数：关键字已转义成 LIKE 模式，转义符 `\`）。
pub const SESSIONS_MENTIONING: &str = "SELECT m.session_id FROM messages m JOIN message_texts t ON t.message_id = m.id \
     WHERE t.text LIKE ? ESCAPE '\\'";

/// 关键字按字面匹配的 LIKE 模式（`%` `_` `\` 转义）。
pub fn like_pattern(keyword: &str) -> String {
    let escaped = keyword.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_");
    format!("%{escaped}%")
}

// ---- 压缩 ----

#[derive(Debug, Clone, PartialEq)]
pub struct Compaction {
    pub id: i64,
    pub through_message_id: i64,
    /// 写入时会话最后一条消息：在它之前的回复报告的 token 数反映的是压缩前的上下文
    pub at_message_id: i64,
    pub summary: String,
    pub created_at: DateTime<Utc>,
}

fn compactions(c: &Connection, filter: &str, session_id: &str) -> Result<Vec<Compaction>> {
    let sql = format!(
        "SELECT id, through_message_id, at_message_id, summary, created_at FROM compactions WHERE session_id = ?1 {filter}"
    );
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt.query_map([session_id], |r| {
        Ok(Compaction {
            id: r.get(0)?,
            through_message_id: r.get(1)?,
            at_message_id: r.get(2)?,
            summary: r.get(3)?,
            created_at: r.get(4)?,
        })
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

pub fn compactions_in(c: &Connection, session_id: &str) -> Result<Vec<Compaction>> {
    compactions(c, "ORDER BY id", session_id)
}

pub fn latest_compaction(c: &Connection, session_id: &str) -> Result<Option<Compaction>> {
    Ok(compactions(c, "ORDER BY id DESC LIMIT 1", session_id)?.pop())
}

/// 记下一次压缩：through_message_id 及之前的消息由摘要代替发给模型。
pub fn add_compaction_in(c: &Connection, session_id: &str, through_message_id: i64, summary: &str) -> Result<()> {
    let at = last_message_id_in(c, session_id)?
        .ok_or_else(|| broken(format!("compacting session {session_id} without messages")))?;
    c.execute(
        "INSERT INTO compactions (session_id, through_message_id, at_message_id, summary, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![session_id, through_message_id, at, summary, Utc::now()],
    )?;
    Ok(())
}

/// 发给模型的会话上下文。
#[derive(Debug, Clone, PartialEq)]
pub struct SessionContext {
    pub summary: Option<String>,
    pub messages: Vec<Message>,
    /// 最近一次压缩写入之前就有的、报告了用量的回复条数：它们报告的是压缩前的大小；没压缩过为 None
    pub stale_responses: Option<usize>,
}

/// 发给模型的上下文，由最近一次压缩与原始历史（切点之前的可有可无）推导，唯一规则：运行加载上下文与会话页算上下文大小
/// 都经这里。切点落在一条回复之前时补一条接续的用户消息（提供方要求对话从用户消息开始）。
pub fn context_of(compaction: Option<&Compaction>, saved: &[Stored]) -> SessionContext {
    let after = compaction.map_or(0, |c| c.through_message_id);
    let kept: Vec<&Stored> = saved.iter().filter(|s| s.id > after).collect();
    let mut messages: Vec<Message> = kept.iter().map(|s| s.message.clone()).collect();
    let stale = compaction.map(|c| {
        let before: Vec<Message> = kept.iter().filter(|s| s.id <= c.at_message_id).map(|s| s.message.clone()).collect();
        measured_responses(&before).len()
    });
    if matches!(messages.first(), Some(Message::Assistant(_))) {
        messages.insert(
            0,
            Message::User(Request {
                parts: vec![RequestPart::Text { text: CONTINUATION.to_owned() }],
                timestamp: Utc::now(),
                metadata: None,
            }),
        );
    }
    SessionContext { summary: compaction.map(|c| c.summary.clone()), messages, stale_responses: stale }
}

pub fn load_context(c: &Connection, session_id: &str) -> Result<SessionContext> {
    let compaction = latest_compaction(c, session_id)?;
    let rows = rows_after(c, session_id, compaction.as_ref().map_or(0, |x| x.through_message_id))?;
    Ok(context_of(compaction.as_ref(), &rows))
}
