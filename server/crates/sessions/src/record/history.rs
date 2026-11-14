//! 会话历史（`messages`、`message_texts`、`compactions` 的唯一读写处）：发给模型的上下文是最新摘要 + 压缩切点之后的原始
//! 消息。原始消息不删除，压缩只决定从哪里开始发。

use muliao_platform::ids::{MessageId, RunId, SessionId};
use std::collections::BTreeMap;

use agenter::{Message, Request, RequestPart};
use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, params};

use crate::record::conventions::{
    CONTINUATION, answer_text, check_metadata, mark_stopped, measured_responses, searchable_text,
};
use muliao_platform::db::{Tx, placeholders};
use muliao_platform::errors::{Result, broken};

/// 一条存下的消息。
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Stored {
    pub id: MessageId,
    pub message: Message,
    pub created_at: DateTime<Utc>,
    /// 保存它的运行
    pub run_id: RunId,
}

/// 读库边界：消息与它各处的 metadata 都在这里检查一次（[`check_metadata`]），之后不再处理坏数据。
fn decode(id: MessageId, payload: &str) -> Result<Message> {
    let message: Message =
        serde_json::from_str(payload).map_err(|e| broken(format!("message {id} does not parse: {e}")))?;
    check_metadata(&message).map_err(|e| broken(format!("message {id} has bad {e}")))?;
    Ok(message)
}

fn stored(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Stored>> {
    let sql = format!("SELECT id, payload, created_at, run_id FROM messages WHERE {filter}");
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt
        .query_map(params, |r| Ok((r.get::<_, MessageId>(0)?, r.get::<_, String>(1)?, r.get(2)?, r.get(3)?)))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter()
        .map(|(id, payload, created_at, run_id)| Ok(Stored { id, message: decode(id, &payload)?, created_at, run_id }))
        .collect()
}

/// 把这些消息接在会话历史末尾（run_id：保存它们的运行），同一事务写可搜索文字。返回写下的消息 id。
pub(crate) fn append_in(
    c: &Tx<'_>,
    session_id: &SessionId,
    run_id: RunId,
    messages: &[Message],
) -> Result<Vec<MessageId>> {
    let now = Utc::now();
    let mut ids = Vec::new();
    for m in messages {
        c.execute(
            "INSERT INTO messages (session_id, run_id, payload, created_at) VALUES (?1, ?2, ?3, ?4)",
            params![session_id, run_id, serde_json::to_string(m).expect("messages serialize"), now],
        )?;
        let id = MessageId::new(c.last_insert_rowid());
        let text = searchable_text(m);
        if !text.is_empty() {
            c.execute("INSERT INTO message_texts (message_id, text) VALUES (?1, ?2)", params![id, text])?;
        }
        ids.push(id);
    }
    Ok(ids)
}

/// 给历史最后一条消息标上停止。
pub(crate) fn mark_last_stopped_in(c: &Tx<'_>, session_id: &SessionId) -> Result<()> {
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

pub(crate) fn last_message_id_in(c: &Connection, session_id: &SessionId) -> Result<Option<MessageId>> {
    Ok(c.query_row("SELECT max(id) FROM messages WHERE session_id = ?1", [session_id], |r| r.get(0))?)
}

/// 保存历史最后一条消息的运行（续跑接管时看停下的部分是谁保存的）。
pub(crate) fn last_saved_by_in(c: &Connection, session_id: &SessionId) -> Result<Option<RunId>> {
    Ok(c.query_row("SELECT run_id FROM messages WHERE session_id = ?1 ORDER BY id DESC LIMIT 1", [session_id], |r| {
        r.get(0)
    })
    .optional()?)
}

/// 这次运行（含之前几次尝试）保存的消息，按顺序。
pub(crate) fn saved_by_in(c: &Connection, session_id: &SessionId, run_id: RunId) -> Result<Vec<Stored>> {
    stored(c, "session_id = ?1 AND run_id = ?2 ORDER BY id", params![session_id, run_id])
}

/// 完整原始历史（不受压缩影响）；since 只取这之后保存的。
pub(crate) fn transcript_in(
    c: &Connection,
    session_id: &SessionId,
    since: Option<DateTime<Utc>>,
) -> Result<Vec<Stored>> {
    stored(c, "session_id = ?1 AND (?2 IS NULL OR created_at >= ?2) ORDER BY id", params![session_id, since])
}

/// 在 after 之后的消息（没有 after 为全部），按顺序。
pub(super) fn rows_after(c: &Connection, session_id: &SessionId, after: Option<MessageId>) -> Result<Vec<Stored>> {
    // 有无游标写成两条 SQL：`?2 IS NULL OR id > ?2` 会让 SQLite 用不上 (session_id, id) 索引的范围查找
    match after {
        Some(after) => stored(c, "session_id = ?1 AND id > ?2 ORDER BY id", params![session_id, after]),
        None => stored(c, "session_id = ?1 ORDER BY id", [session_id]),
    }
}

/// 从最后一条模型回复起的消息（还没有回复时是全部）。之前每条回复的调用在下一条回复之前都已有结果，所以还没有结果的
/// 调用只可能在这一段里：查它们不必加载整段上下文。
pub(crate) fn since_last_response(c: &Connection, session_id: &SessionId) -> Result<Vec<Message>> {
    let mut tail: Vec<Message> = Vec::new();
    let mut before: Option<MessageId> = None;
    loop {
        // 分批往回读：通常一两条就到最后一条回复
        let rows = match before {
            Some(before) => {
                stored(c, "session_id = ?1 AND id < ?2 ORDER BY id DESC LIMIT 4", params![session_id, before])?
            }
            None => stored(c, "session_id = ?1 ORDER BY id DESC LIMIT 4", [session_id])?,
        };
        let n = rows.len();
        for row in rows {
            let is_response = matches!(row.message, Message::Assistant(_));
            before = Some(row.id);
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
pub(crate) fn since_last_response_among(
    c: &Connection,
    session_ids: &[SessionId],
) -> Result<BTreeMap<SessionId, Vec<Message>>> {
    let mut tails: BTreeMap<SessionId, Vec<Message>> = session_ids.iter().map(|s| (s.clone(), Vec::new())).collect();
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
            Ok((r.get::<_, SessionId>(0)?, r.get::<_, MessageId>(1)?, r.get::<_, String>(2)?))
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
pub(crate) fn latest_answer(c: &Connection, session_id: &SessionId) -> Result<String> {
    Ok(answer_of(&since_last_response(c, session_id)?))
}

pub(crate) fn latest_answers_among(c: &Connection, session_ids: &[SessionId]) -> Result<BTreeMap<SessionId, String>> {
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

pub(crate) fn spans_between_in(
    c: &Connection,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
) -> Result<BTreeMap<SessionId, Span>> {
    let mut stmt = c.prepare(
        "SELECT session_id, count(*), min(created_at), max(created_at) FROM messages \
         WHERE created_at >= ?1 AND created_at < ?2 GROUP BY session_id",
    )?;
    let rows = stmt.query_map(params![start, end], |r| {
        Ok((r.get::<_, SessionId>(0)?, Span { messages: r.get(1)?, first_at: r.get(2)?, last_at: r.get(3)? }))
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

/// 可搜索文字里提到关键字的会话的 SQL 子查询（一个参数：关键字已转义成 LIKE 模式，转义符 `\`）。会话列表按关键字筛选时
/// 嵌进 sessions 的查询：只碰本模块的表，由本模块给出；不拆成先取 id 再 `IN (…)`，常见词会命中成千上万个会话。
pub(crate) const SESSIONS_MENTIONING: &str = "SELECT m.session_id FROM messages m JOIN message_texts t ON t.message_id = m.id \
     WHERE t.text LIKE ? ESCAPE '\\'";

// ---- 压缩 ----

#[derive(Debug, Clone, PartialEq)]
pub struct Compaction {
    pub id: i64,
    pub through_message_id: MessageId,
    /// 写入时会话最后一条消息：在它之前的回复报告的 token 数反映的是压缩前的上下文
    pub at_message_id: MessageId,
    pub summary: String,
    pub created_at: DateTime<Utc>,
}

fn compactions(c: &Connection, filter: &str, session_id: &SessionId) -> Result<Vec<Compaction>> {
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

pub(crate) fn compactions_in(c: &Connection, session_id: &SessionId) -> Result<Vec<Compaction>> {
    compactions(c, "ORDER BY id", session_id)
}

pub(crate) fn latest_compaction(c: &Connection, session_id: &SessionId) -> Result<Option<Compaction>> {
    Ok(compactions(c, "ORDER BY id DESC LIMIT 1", session_id)?.pop())
}

/// 记下一次压缩：through_message_id 及之前的消息由摘要代替发给模型。
pub(super) fn add_compaction_in(
    c: &Tx<'_>,
    session_id: &SessionId,
    through_message_id: MessageId,
    summary: &str,
) -> Result<()> {
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
pub(crate) struct SessionContext {
    pub summary: Option<String>,
    pub messages: Vec<Message>,
    /// 最近一次压缩写入之前就有的、报告了用量的回复条数：它们报告的是压缩前的大小；没压缩过为 None
    pub stale_responses: Option<usize>,
}

/// 发给模型的上下文，由最近一次压缩与原始历史（切点之前的可有可无）推导，唯一规则：运行加载上下文与会话页算上下文大小
/// 都经这里。切点落在一条回复之前时补一条接续的用户消息（提供方要求对话从用户消息开始）。
pub(crate) fn context_of(compaction: Option<&Compaction>, saved: &[Stored]) -> SessionContext {
    let after = compaction.map(|c| c.through_message_id);
    let kept: Vec<&Stored> = saved.iter().filter(|s| after.is_none_or(|a| s.id > a)).collect();
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

pub(crate) fn load_context(c: &Connection, session_id: &SessionId) -> Result<SessionContext> {
    let compaction = latest_compaction(c, session_id)?;
    let rows = rows_after(c, session_id, compaction.as_ref().map(|x| x.through_message_id))?;
    Ok(context_of(compaction.as_ref(), &rows))
}
