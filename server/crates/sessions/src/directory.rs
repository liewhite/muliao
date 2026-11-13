//! 会话目录（`sessions` 表的唯一读写处）：新建一行、按属性查会话（按发送方定位、按 agent、thread、列表）。会话的规则
//! （来源、续写）在 [`crate::origins`] 与 [`crate::inbound`]，这里只是查询与写入。

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use rusqlite::{Connection, Row, params};

use crate::history::{SESSIONS_MENTIONING, like_pattern};
use crate::mailbox::placeholders;
use crate::origins::{Origin, Sender, SessionOrigin};
use muliao_platform::errors::Result;

/// 一个会话（读模型）。
#[derive(Debug, Clone, PartialEq)]
pub struct Session {
    pub id: String,
    pub agent_id: String,
    pub title: String,
    pub origin: SessionOrigin,
    /// 委派开出的 thread 的父会话（删除后为空）
    pub parent_session_id: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

impl Session {
    pub fn is_thread(&self) -> bool {
        self.origin.thread().is_some()
    }
}

const COLUMNS: &str = "id, agent_id, title, origin, origin_ref, parent_session_id, thread, created_at, updated_at";

type Raw =
    (String, String, String, String, Option<String>, Option<String>, Option<String>, DateTime<Utc>, DateTime<Utc>);

fn raw(r: &Row<'_>) -> rusqlite::Result<Raw> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?))
}

fn session((id, agent_id, title, origin, reference, parent, thread, created_at, updated_at): Raw) -> Result<Session> {
    Ok(Session {
        origin: SessionOrigin::parse(&origin, reference.as_deref(), thread)?,
        id,
        agent_id,
        title,
        parent_session_id: parent,
        created_at,
        updated_at,
    })
}

pub(crate) fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Session>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM sessions WHERE {filter}"))?;
    let rows = stmt.query_map(params, raw)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(session).collect()
}

/// 新会话的描述（规则由唯一的入口 [`crate::inbound`] 检查）。
pub(crate) struct NewSession<'a> {
    pub id: &'a str,
    pub agent_id: &'a str,
    pub title: &'a str,
    pub origin: SessionOrigin,
    pub parent_session_id: Option<&'a str>,
}

pub(crate) fn add_in(c: &Connection, s: NewSession<'_>) -> Result<Session> {
    let now = Utc::now();
    let reference = s.origin.sender().map(Sender::reference);
    c.execute(
        "INSERT INTO sessions (id, agent_id, title, origin, origin_ref, parent_session_id, thread, created_at, updated_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8)",
        params![s.id, s.agent_id, s.title, s.origin.origin().as_str(), reference, s.parent_session_id, s.origin.thread(), now],
    )?;
    Ok(get_in(c, s.id)?.expect("the session was just written"))
}

pub fn get_in(c: &Connection, session_id: &str) -> Result<Option<Session>> {
    Ok(query(c, "id = ?1", [session_id])?.pop())
}

/// 正在运行的会话（运行的开头、结束、失败时取）。忙的会话不会被删除（[`crate::deletion`]），所以不在是 bug，报 Broken。
pub(crate) fn running_in(c: &Connection, session_id: &str) -> Result<Session> {
    get_in(c, session_id)?.ok_or_else(|| {
        muliao_platform::errors::broken(format!(
            "session {session_id} vanished while it had work: busy sessions are not deleted"
        ))
    })
}

/// 续写或运行做完时更新会话的最近时间（列表按它排序）。
pub(crate) fn touch_in(c: &Connection, session_id: &str) -> Result<()> {
    c.execute("UPDATE sessions SET updated_at = ?1 WHERE id = ?2", params![Utc::now(), session_id])?;
    Ok(())
}

/// 这些会话（已删除的不在结果里），一次查询。
pub fn among_in(c: &Connection, session_ids: &[String]) -> Result<BTreeMap<String, Session>> {
    if session_ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let filter = format!("id IN ({})", placeholders(session_ids.len()));
    Ok(query(c, &filter, rusqlite::params_from_iter(session_ids))?.into_iter().map(|s| (s.id.clone(), s)).collect())
}

/// id 以它开头的会话，至多 limit 个（热历史的工具接受 id 前缀）。
pub fn with_prefix_in(c: &Connection, prefix: &str, limit: usize) -> Result<Vec<Session>> {
    let pattern = like_pattern(prefix).trim_start_matches('%').to_owned();
    query(c, "id LIKE ?1 ESCAPE '\\' ORDER BY id LIMIT ?2", params![pattern, limit as i64])
}

/// agent 的全部会话。
pub fn of_agent_in(c: &Connection, agent_id: &str) -> Result<Vec<Session>> {
    query(c, "agent_id = ?1", [agent_id])
}

/// 按发送方定位会话的唯一规则：(发送方, agent id) -> 这个发送方给这个 agent 开过的最新一个顶层会话，一次查询；给出
/// agent_id 时只看这个 agent。thread 没有发送方身份，不会被定位到。
pub fn keyed_among(
    c: &Connection,
    senders: &[Sender],
    agent_id: Option<&str>,
) -> Result<BTreeMap<(Sender, String), Session>> {
    let mut found = BTreeMap::new();
    if senders.is_empty() {
        return Ok(found);
    }
    let mut conditions = Vec::new();
    let mut values: Vec<String> = Vec::new();
    for s in senders {
        conditions.push(format!("(origin = ?{} AND origin_ref = ?{})", values.len() + 1, values.len() + 2));
        values.push(s.origin().as_str().to_owned());
        values.push(s.reference());
    }
    let mut filter = format!("({})", conditions.join(" OR "));
    if let Some(a) = agent_id {
        values.push(a.to_owned());
        filter.push_str(&format!(" AND agent_id = ?{}", values.len()));
    }
    filter.push_str(" ORDER BY created_at, id");
    for s in query(c, &filter, rusqlite::params_from_iter(&values))? {
        let sender = s.origin.sender().expect("matched by an origin with a ref");
        // 按创建先后，后面的覆盖前面的：留下最新的
        found.insert((sender, s.agent_id.clone()), s);
    }
    Ok(found)
}

/// 父会话开出的 thread，按创建顺序。
pub fn threads_of(c: &Connection, parent_id: &str) -> Result<Vec<Session>> {
    query(c, "parent_session_id = ?1 ORDER BY created_at, id", [parent_id])
}

/// thread 由（父会话, agent, 名字）确定。
pub fn thread_in(c: &Connection, parent_id: &str, agent_id: &str, thread: &str) -> Result<Option<Session>> {
    Ok(query(c, "parent_session_id = ?1 AND agent_id = ?2 AND thread = ?3", params![parent_id, agent_id, thread])?
        .pop())
}

/// 父会话里叫这个名字的 thread（每个 agent 至多一个），按创建顺序。
pub fn threads_named_in(c: &Connection, parent_id: &str, thread: &str) -> Result<Vec<Session>> {
    query(c, "parent_session_id = ?1 AND thread = ?2 ORDER BY created_at, id", params![parent_id, thread])
}

/// 会话列表的筛选：按 agent、来源、发送方身份、关键字；都不给就是全部会话。
#[derive(Debug, Clone, Default)]
pub struct Listing {
    pub agent_id: Option<String>,
    pub origins: Option<Vec<Origin>>,
    pub sender: Option<Sender>,
    /// 标题或任一条消息的可搜索文字含有它（不区分 ASCII 大小写）
    pub keyword: Option<String>,
}

impl Listing {
    fn filter(&self) -> (String, Vec<String>) {
        let mut conditions = vec!["1".to_owned()];
        let mut values: Vec<String> = Vec::new();
        if let Some(a) = &self.agent_id {
            values.push(a.clone());
            conditions.push("agent_id = ?".into());
        }
        if let Some(origins) = &self.origins {
            conditions.push(format!("origin IN ({})", placeholders(origins.len().max(1))));
            if origins.is_empty() {
                values.push(String::new());
            }
            values.extend(origins.iter().map(|o| o.as_str().to_owned()));
        }
        if let Some(s) = &self.sender {
            conditions.push("origin = ? AND origin_ref = ?".into());
            values.push(s.origin().as_str().to_owned());
            values.push(s.reference());
        }
        if let Some(k) = &self.keyword {
            conditions.push(format!("(title LIKE ? ESCAPE '\\' OR id IN ({SESSIONS_MENTIONING}))"));
            values.push(like_pattern(k));
            values.push(like_pattern(k));
        }
        (conditions.join(" AND "), values)
    }
}

/// 符合筛选的全部会话 id。
pub fn listed_ids_in(c: &Connection, listing: &Listing) -> Result<Vec<String>> {
    let (filter, values) = listing.filter();
    let mut stmt = c.prepare(&format!("SELECT id FROM sessions WHERE {filter}"))?;
    Ok(stmt.query_map(rusqlite::params_from_iter(&values), |r| r.get(0))?.collect::<rusqlite::Result<_>>()?)
}

/// 列表的一页：first 里的会话置顶，其余按最近更新排序。
pub fn listed_page_in(
    c: &Connection,
    listing: &Listing,
    first: &[String],
    limit: usize,
    offset: usize,
) -> Result<Vec<Session>> {
    let (filter, mut values) = listing.filter();
    // 常数不能做排序项（SQLite 把整数当成第几列），没有置顶的就只按更新时间
    let pinned =
        if first.is_empty() { String::new() } else { format!("(id IN ({})) DESC, ", placeholders(first.len())) };
    values.extend(first.iter().cloned());
    let sql = format!("{filter} ORDER BY {pinned}updated_at DESC LIMIT {limit} OFFSET {offset}");
    query(c, &sql, rusqlite::params_from_iter(&values))
}

/// 删掉这些会话（规则在 [`crate::deletion`]）。它们开出的 thread 保留（父引用置空），其余行随之级联删除。
pub(crate) fn delete_in(c: &Connection, session_ids: &[String]) -> Result<()> {
    for id in session_ids {
        c.execute("DELETE FROM sessions WHERE id = ?1", [id])?;
    }
    Ok(())
}
