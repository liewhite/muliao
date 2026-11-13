//! 冷历史：agent 做过的事，一件一行，只追加（`agent_history` 表的唯一读写处）；按时间范围、重要程度与关键词检索。
//!
//! 写入方是 agent 自己（工具 history_append，只能写自己的），读取方是 agent（history_search、activity_overview）与界面。
//! 行不能改也不能删（库里的触发器拦住，见迁移），所以没有事件，也不参与删除 agent 的清理。

use std::sync::Arc;

use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use chrono_tz::Tz;
use muliao_platform::db::Database;
use muliao_platform::db::like_contains;
use rusqlite::{Connection, ToSql, params};
use serde::Serialize;

use muliao_platform::errors::{Result, invalid};

/// trigram 分词下 FTS5 只能匹配至少 3 个字符的词，更短的词改用 LIKE
const TRIGRAM: usize = 3;
const MAX_TITLE: usize = 200;
/// 重要程度：1 一般，2 重要，3 很重要
const IMPORTANCE: std::ops::RangeInclusive<i64> = 1..=3;
/// 检索条数：不给时的默认与上限（工具 history_search 与界面的接口共用）
pub const DEFAULT_LIMIT: usize = 20;
pub const MAX_LIMIT: usize = 200;

/// 要写入的一件事。
#[derive(Debug, Clone, PartialEq)]
pub struct NewEntry {
    /// 工作发生的那天（配置时区的日历日）
    pub day: NaiveDate,
    pub title: String,
    /// 2–5 行：做了什么、结果、重点
    pub summary: String,
    /// 引用：#issue、会话 id、commit 等
    pub refs: Vec<String>,
    pub importance: i64,
}

/// 库里的一件事。
#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct Record {
    pub id: i64,
    pub agent_id: String,
    pub day: NaiveDate,
    pub title: String,
    pub summary: String,
    pub refs: Vec<String>,
    pub importance: i64,
    pub created_at: DateTime<Utc>,
}

/// 一次检索。没给 query 按日期倒序（同一天按写入倒序）；给了按空白切词、每个词都要出现在标题、摘要或引用里：词都不短于
/// 3 个字符时用 FTS5（trigram）按 bm25 相关度排序，否则逐词 LIKE、按日期倒序。
#[derive(Debug, Clone, PartialEq)]
pub struct Search {
    /// None 为全部 agent
    pub agent_ids: Option<Vec<String>>,
    pub query: Option<String>,
    /// 含两端的日期
    pub since: Option<NaiveDate>,
    pub until: Option<NaiveDate>,
    pub min_importance: i64,
    pub limit: usize,
}

impl Search {
    /// 这些 agent（None 为全部）的历史，其余条件取默认。
    pub fn of(agent_ids: Option<Vec<String>>) -> Self {
        Search { agent_ids, query: None, since: None, until: None, min_importance: 1, limit: DEFAULT_LIMIT }
    }
}

/// 冷历史。克隆共享同一份。
#[derive(Clone)]
pub struct History {
    db: Arc<Database>,
    tz: Tz,
}

impl History {
    /// tz：历史的日历日所在的时区（配置的时区）。
    pub fn new(db: Arc<Database>, tz: Tz) -> Self {
        History { db, tz }
    }

    pub fn db(&self) -> &Arc<Database> {
        &self.db
    }

    pub fn tz(&self) -> Tz {
        self.tz
    }

    pub fn today(&self) -> NaiveDate {
        Utc::now().with_timezone(&self.tz).date_naive()
    }

    /// 这个本地日历日的起止时刻 [start, end)：活动概览按它取当天的会话活动。
    pub fn day_bounds(&self, day: NaiveDate) -> (DateTime<Utc>, DateTime<Utc>) {
        // 一天从本地零点开始；零点在夏令时切换里被跳过的时区，从之后第一个存在的整点开始
        let start = |d: NaiveDate| {
            (0..24)
                .find_map(|h| self.tz.from_local_datetime(&d.and_hms_opt(h, 0, 0)?).earliest())
                .expect("a local day has an hour that exists")
                .with_timezone(&Utc)
        };
        (start(day), start(day.succ_opt().expect("a day after")))
    }

    /// 给 agent 追加若干件事（一个事务，全部合法才写入）。
    pub async fn append(&self, agent_id: &str, entries: Vec<NewEntry>) -> Result<Vec<Record>> {
        if entries.is_empty() {
            return Err(invalid("no entries to append"));
        }
        let today = self.today();
        for e in &entries {
            check_entry(e, today)?;
        }
        let agent_id = agent_id.to_owned();
        self.db
            .write(move |tx| {
                let now = Utc::now();
                entries
                    .into_iter()
                    .map(|e| {
                        let (title, summary) = (e.title.trim().to_owned(), e.summary.trim().to_owned());
                        let refs = serde_json::to_string(&e.refs).expect("strings serialize");
                        tx.execute(
                            "INSERT INTO agent_history (agent_id, day, title, summary, refs, importance, created_at) \
                             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                            params![agent_id, e.day, title, summary, refs, e.importance, now],
                        )?;
                        Ok(Record {
                            id: tx.last_insert_rowid(),
                            agent_id: agent_id.clone(),
                            day: e.day,
                            title,
                            summary,
                            refs: e.refs,
                            importance: e.importance,
                            created_at: now,
                        })
                    })
                    .collect::<Result<Vec<_>>>()
            })
            .await
    }

    pub async fn search(&self, search: Search) -> Result<Vec<Record>> {
        self.db.read(move |c| search_in(c, &search)).await
    }
}

/// 一条要写入的事是否合乎契约；不合乎报领域错误，带上是哪条、哪里不对。
fn check_entry(e: &NewEntry, today: NaiveDate) -> Result<()> {
    let at = format!("entry for {} {:?}", e.day, e.title);
    if e.title.trim().is_empty() {
        return Err(invalid(format!("{at}: title is empty")));
    }
    if e.title.chars().count() > MAX_TITLE {
        return Err(invalid(format!("{at}: title is longer than {MAX_TITLE} characters")));
    }
    if e.summary.trim().is_empty() {
        return Err(invalid(format!("{at}: summary is empty")));
    }
    if !IMPORTANCE.contains(&e.importance) {
        return Err(invalid(format!("{at}: importance must be 1, 2 or 3, got {}", e.importance)));
    }
    if e.day > today {
        return Err(invalid(format!("{at}: day is after today ({today}); record work that already happened")));
    }
    Ok(())
}

/// FTS5 的 MATCH 表达式：每个词作为一个短语（双引号转义），词之间是 AND。
fn match_expression(terms: &[&str]) -> String {
    terms.iter().map(|t| format!("\"{}\"", t.replace('"', "\"\""))).collect::<Vec<_>>().join(" ")
}

/// 检索历史（[`Search`]）；读连接里调用，活动概览与别的读放在一起。
pub fn search_in(c: &Connection, s: &Search) -> Result<Vec<Record>> {
    if !(1..=MAX_LIMIT).contains(&s.limit) {
        return Err(invalid(format!("limit must be between 1 and {MAX_LIMIT}, got {}", s.limit)));
    }
    if !IMPORTANCE.contains(&s.min_importance) {
        return Err(invalid(format!("min_importance must be 1, 2 or 3, got {}", s.min_importance)));
    }
    if let (Some(since), Some(until)) = (s.since, s.until)
        && since > until
    {
        return Err(invalid(format!("since ({since}) is after until ({until})")));
    }
    let terms: Option<Vec<&str>> = s.query.as_deref().map(|q| q.split_whitespace().collect());
    if terms.as_ref().is_some_and(Vec::is_empty) {
        return Err(invalid("query is empty; omit it to list by date"));
    }
    let mut values: Vec<Box<dyn ToSql>> = Vec::new();
    let mut bind = |v: Box<dyn ToSql>| {
        values.push(v);
        format!("?{}", values.len())
    };
    let mut from = "agent_history h".to_owned();
    let mut filters = vec![format!("h.importance >= {}", bind(Box::new(s.min_importance)))];
    if let Some(ids) = &s.agent_ids {
        let marks: Vec<String> = ids.iter().map(|id| bind(Box::new(id.clone()))).collect();
        filters.push(format!("h.agent_id IN ({})", marks.join(", ")));
    }
    if let Some(since) = s.since {
        filters.push(format!("h.day >= {}", bind(Box::new(since))));
    }
    if let Some(until) = s.until {
        filters.push(format!("h.day <= {}", bind(Box::new(until))));
    }
    let by_date = "h.day DESC, h.id DESC";
    let order = match &terms {
        None => by_date.to_owned(),
        Some(terms) if terms.iter().all(|t| t.chars().count() >= TRIGRAM) => {
            from.push_str(" JOIN agent_history_fts ON agent_history_fts.rowid = h.id");
            filters.push(format!("agent_history_fts MATCH {}", bind(Box::new(match_expression(terms)))));
            format!("bm25(agent_history_fts), {by_date}")
        }
        Some(terms) => {
            for t in terms {
                let p = bind(Box::new(like_contains(t)));
                // 引用按 JSON 解码后的值匹配（原文里有括号引号，非 ASCII 字符可能被转义）
                filters.push(format!(
                    "(h.title LIKE {p} ESCAPE '\\' OR h.summary LIKE {p} ESCAPE '\\' \
                     OR EXISTS (SELECT 1 FROM json_each(h.refs) WHERE value LIKE {p} ESCAPE '\\'))"
                ));
            }
            by_date.to_owned()
        }
    };
    let limit = bind(Box::new(s.limit as i64));
    let sql = format!(
        "SELECT h.id, h.agent_id, h.day, h.title, h.summary, h.refs, h.importance, h.created_at FROM {from} \
         WHERE {} ORDER BY {order} LIMIT {limit}",
        filters.join(" AND ")
    );
    let mut stmt = c.prepare(&sql)?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(values.iter().map(|v| v.as_ref())), |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, NaiveDate>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
                r.get::<_, i64>(6)?,
                r.get::<_, DateTime<Utc>>(7)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter()
        .map(|(id, agent_id, day, title, summary, refs, importance, created_at)| {
            let refs = serde_json::from_str(&refs).map_err(|e| {
                muliao_platform::errors::broken(format!("refs of history entry {id} are not a list of strings: {e}"))
            })?;
            Ok(Record { id, agent_id, day, title, summary, refs, importance, created_at })
        })
        .collect()
}
