//! 模型用量账目：记下每次模型请求花掉的 token，按时间范围汇总，或取一个会话的累计（docs/architecture.md 第 19 节）。
//!
//! 写入方只有调用模型的运行端口（runtime），读取方只有界面：没有事件、不参与别的领域的清理，行只增不改，一直留着。
//! 时间存 UTC（`created_at` 的文本形式按时间先后可比，范围查询直接比较）；按天汇总时按配置的时区切日。

use muliao_platform::ids::{AgentId, SessionId};
use std::collections::BTreeMap;
use std::ops::{Add, AddAssign};
use std::sync::Arc;

use chrono::{DateTime, NaiveDate, Utc};
use chrono_tz::Tz;
use muliao_platform::db::{Database, Migration};

pub const MIGRATION: Migration =
    Migration { domain: "usage", steps: &[include_str!("../migrations/001_model_usage.sql")] };

/// 哪一类运行用掉的 token（运行端口的两个模型调用入口各对应一个）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunKind {
    /// 会话的一次运行
    Run,
    /// 上下文压缩的一次总结
    Compaction,
}

impl RunKind {
    pub fn as_str(self) -> &'static str {
        match self {
            RunKind::Run => "run",
            RunKind::Compaction => "compaction",
        }
    }
}

/// 用量的各项计数（一笔的，或一组的和）。`input_tokens` 是输入总数，**已经包含**缓存读写两项，展示为「总数与其中两部分」，
/// 不要相加。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
pub struct UsageTotals {
    pub requests: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_tokens: u64,
    pub cache_write_tokens: u64,
}

impl Add for UsageTotals {
    type Output = Self;
    fn add(self, o: Self) -> Self {
        Self {
            requests: self.requests + o.requests,
            input_tokens: self.input_tokens + o.input_tokens,
            output_tokens: self.output_tokens + o.output_tokens,
            cache_read_tokens: self.cache_read_tokens + o.cache_read_tokens,
            cache_write_tokens: self.cache_write_tokens + o.cache_write_tokens,
        }
    }
}

impl AddAssign for UsageTotals {
    fn add_assign(&mut self, o: Self) {
        *self = *self + o;
    }
}

/// 一笔用量（一次模型请求，或运行收尾时还没记上的部分）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageEntry {
    pub agent_id: AgentId,
    /// 不属于任何会话的运行为空（历史数据里有）
    pub session_id: Option<SessionId>,
    pub kind: RunKind,
    /// `<provider 名>:<模型名>`，与 agent.toml 的 model 同一格式
    pub model: String,
    pub totals: UsageTotals,
}

/// 汇总时的分组维度，可以任意组合。
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum UsageGroup {
    Agent,
    Model,
    Day,
}

/// 一个分组的汇总。没有选中的维度为 None（例如只按模型分组时 agent_id 与 day 都是 None）。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
pub struct UsageBucket {
    pub agent_id: Option<AgentId>,
    pub model: Option<String>,
    /// 配置时区里的日历日
    pub day: Option<NaiveDate>,
    #[serde(flatten)]
    pub totals: UsageTotals,
}

/// 汇总要用到的那几列。
struct Row {
    created_at: DateTime<Utc>,
    agent_id: AgentId,
    model: String,
    totals: UsageTotals,
}

/// 把若干行按选中的维度相加。分组规则只在这里写一次；「天」按 tz 里的日历日划分（用量要和人看日历的方式对齐，不按 UTC
/// 切日）。结果按 (天, agent, 模型) 排序。
fn summarize_rows(rows: impl IntoIterator<Item = Row>, group_by: &[UsageGroup], tz: Tz) -> Vec<UsageBucket> {
    // (天, agent, 模型)：按它排序
    type Key = (Option<NaiveDate>, Option<AgentId>, Option<String>);
    let mut summed: BTreeMap<Key, UsageTotals> = BTreeMap::new();
    for row in rows {
        let key = (
            group_by.contains(&UsageGroup::Day).then(|| row.created_at.with_timezone(&tz).date_naive()),
            group_by.contains(&UsageGroup::Agent).then_some(row.agent_id),
            group_by.contains(&UsageGroup::Model).then_some(row.model),
        );
        *summed.entry(key).or_default() += row.totals;
    }
    summed.into_iter().map(|((day, agent_id, model), totals)| UsageBucket { agent_id, model, day, totals }).collect()
}

/// 从 `requests` 列起依次是五项计数（与 [`UsageTotals`] 的字段同序）
fn totals_at(r: &rusqlite::Row<'_>, first: usize) -> rusqlite::Result<UsageTotals> {
    Ok(UsageTotals {
        requests: r.get(first)?,
        input_tokens: r.get(first + 1)?,
        output_tokens: r.get(first + 2)?,
        cache_read_tokens: r.get(first + 3)?,
        cache_write_tokens: r.get(first + 4)?,
    })
}

pub struct UsageLog {
    db: Arc<Database>,
    tz: Tz,
}

impl UsageLog {
    pub fn new(db: Arc<Database>, tz: Tz) -> Self {
        Self { db, tz }
    }

    /// 记一笔用量（自己开事务）。返回 Ok 即已入账；调用方在等待中被取消时，这笔照样会入账（事务不被打断），所以调用方
    /// 要把「交出去了」当作已入账，不要并进下一笔重记。
    pub async fn record(&self, entry: UsageEntry) -> rusqlite::Result<()> {
        let at = Utc::now();
        self.db
            .write(move |tx| {
                let t = entry.totals;
                tx.execute(
                    "INSERT INTO model_usage (created_at, agent_id, session_id, kind, model, requests, input_tokens,
                        output_tokens, cache_read_tokens, cache_write_tokens)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
                    rusqlite::params![
                        at,
                        entry.agent_id,
                        entry.session_id,
                        entry.kind.as_str(),
                        entry.model,
                        t.requests,
                        t.input_tokens,
                        t.output_tokens,
                        t.cache_read_tokens,
                        t.cache_write_tokens,
                    ],
                )
                .map(drop)
            })
            .await
    }

    /// [since, until) 之间的用量，按选中的维度汇总；group_by 为空时只有一个总计（没有行时为空列表）。
    pub async fn summarize(
        &self,
        since: DateTime<Utc>,
        until: DateTime<Utc>,
        group_by: &[UsageGroup],
    ) -> rusqlite::Result<Vec<UsageBucket>> {
        let rows = self
            .db
            .read(move |c| {
                let mut stmt = c.prepare_cached(
                    "SELECT created_at, agent_id, model, requests, input_tokens, output_tokens, cache_read_tokens,
                        cache_write_tokens
                     FROM model_usage WHERE created_at >= ?1 AND created_at < ?2",
                )?;
                stmt.query_map(rusqlite::params![since, until], |r| {
                    Ok(Row { created_at: r.get(0)?, agent_id: r.get(1)?, model: r.get(2)?, totals: totals_at(r, 3)? })
                })?
                .collect::<rusqlite::Result<Vec<_>>>()
            })
            .await?;
        Ok(summarize_rows(rows, group_by, self.tz))
    }

    /// 这个会话名下全部行（不分运行类型）的用量之和；还没有记过时各项为 0。
    pub async fn session_totals(&self, session_id: &SessionId) -> rusqlite::Result<UsageTotals> {
        let session_id = session_id.clone();
        self.db
            .read(move |c| {
                c.query_row(
                    "SELECT coalesce(sum(requests), 0), coalesce(sum(input_tokens), 0),
                        coalesce(sum(output_tokens), 0), coalesce(sum(cache_read_tokens), 0),
                        coalesce(sum(cache_write_tokens), 0)
                     FROM model_usage WHERE session_id = ?1",
                    [session_id],
                    |r| totals_at(r, 0),
                )
            })
            .await
    }
}

#[cfg(test)]
mod tests;
