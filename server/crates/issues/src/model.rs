//! issue 本身（`issues` 表的唯一读写处）与它的取值：状态、优先级、指派人、做事的人。

use muliao_platform::ids::{AgentId, SessionId};
use std::fmt;

use chrono::{DateTime, Utc};
use muliao_assets::naming::USER;
use rusqlite::{Connection, OptionalExtension, Row, params};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use muliao_platform::errors::{Result, not_found};

// 状态。不设 todo（给 agent 的一建就投，与 in_progress 无别）、不设 waiting：在等上游由依赖派生，在等后台任务、委派、
// 唤醒由 issue 会话的工作状态看出（#258、#261）。工具参数的 schema 用到它：类型与变体上只写 `//` 注释，免得说明进 schema
#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    Hash,
    PartialOrd,
    Ord,
    Default,
    Serialize,
    Deserialize,
    JsonSchema,
    utoipa::ToSchema,
)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    #[default]
    Backlog,
    // 该由被指派的 agent 做：动态投给它
    InProgress,
    InReview,
    Blocked,
    Done,
    Cancelled,
}

impl Status {
    pub const ALL: [Status; 6] =
        [Status::Backlog, Status::InProgress, Status::InReview, Status::Blocked, Status::Done, Status::Cancelled];

    pub fn as_str(self) -> &'static str {
        match self {
            Status::Backlog => "backlog",
            Status::InProgress => "in_progress",
            Status::InReview => "in_review",
            Status::Blocked => "blocked",
            Status::Done => "done",
            Status::Cancelled => "cancelled",
        }
    }

    /// 存下的取值；时间线里 #258 之前的 todo、waiting 不在其中（那里按文字存，见 [`crate::activity::Happening::Status`]）。
    pub fn parse(s: &str) -> Option<Status> {
        Status::ALL.into_iter().find(|v| v.as_str() == s)
    }

    /// 已结束（done / cancelled）：不再挡着依赖它的 issue（取消也放行，下游自己判断还做不做）。
    pub fn finished(self) -> bool {
        matches!(self, Status::Done | Status::Cancelled)
    }
}

impl fmt::Display for Status {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl rusqlite::types::FromSql for Status {
    fn column_result(value: rusqlite::types::ValueRef<'_>) -> rusqlite::types::FromSqlResult<Self> {
        let raw = value.as_str()?;
        Status::parse(raw).ok_or_else(|| rusqlite::types::FromSqlError::Other(format!("unknown status {raw:?}").into()))
    }
}

/// 时间线里记下的状态：现有的取值，或 #258 之前去掉的（[`Retired`]，只在旧动态里；时间线只增不改）。存成文字。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(untagged)]
pub enum Recorded {
    Now(Status),
    Retired(Retired),
}

/// #258 去掉的状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum Retired {
    Todo,
    Waiting,
}

impl Recorded {
    /// 已结束（done / cancelled）。
    pub fn finished(&self) -> bool {
        matches!(self, Recorded::Now(s) if s.finished())
    }
}

impl fmt::Display for Recorded {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Recorded::Now(s) => f.write_str(s.as_str()),
            Recorded::Retired(Retired::Todo) => f.write_str("todo"),
            Recorded::Retired(Retired::Waiting) => f.write_str("waiting"),
        }
    }
}

impl From<Status> for Recorded {
    fn from(s: Status) -> Self {
        Recorded::Now(s)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize, JsonSchema, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum Priority {
    #[default]
    None,
    Urgent,
    High,
    Medium,
    Low,
}

impl Priority {
    pub const ALL: [Priority; 5] = [Priority::None, Priority::Urgent, Priority::High, Priority::Medium, Priority::Low];

    pub fn as_str(self) -> &'static str {
        match self {
            Priority::None => "none",
            Priority::Urgent => "urgent",
            Priority::High => "high",
            Priority::Medium => "medium",
            Priority::Low => "low",
        }
    }

    pub fn parse(s: &str) -> Option<Priority> {
        Priority::ALL.into_iter().find(|v| v.as_str() == s)
    }
}

impl fmt::Display for Priority {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl rusqlite::types::FromSql for Priority {
    fn column_result(value: rusqlite::types::ValueRef<'_>) -> rusqlite::types::FromSqlResult<Self> {
        let raw = value.as_str()?;
        Priority::parse(raw)
            .ok_or_else(|| rusqlite::types::FromSqlError::Other(format!("unknown priority {raw:?}").into()))
    }
}

/// 指派人：用户（只有一个），或一个 agent。库里存成 `user` 或 agent id（12 位十六进制，不会与 `user` 相同；agent 名称也
/// 不能取 `user`，工具里按名称指派时不会混淆），读写库只经 [`Self::as_str`] / [`Self::from_stored`]；JSON（接口与时间线的
/// change）按种类打标签。未指派是 `Option::None`。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Assignee {
    User,
    Agent { agent_id: AgentId },
}

impl Assignee {
    pub fn as_str(&self) -> &str {
        match self {
            Assignee::User => USER,
            Assignee::Agent { agent_id } => agent_id.as_str(),
        }
    }

    /// 库里存的形式（[`Self::as_str`]）读回来。
    pub fn from_stored(s: String) -> Self {
        if s == USER { Assignee::User } else { Assignee::Agent { agent_id: AgentId::new(s) } }
    }

    pub fn agent(&self) -> Option<&AgentId> {
        match self {
            Assignee::User => None,
            Assignee::Agent { agent_id } => Some(agent_id),
        }
    }
}

/// 指派给 agent 时是它的 id；未指派或指派给用户为 None。
pub fn agent_of(assignee: Option<&Assignee>) -> Option<&AgentId> {
    assignee.and_then(Assignee::agent)
}

/// 时间线上「系统」做的动作（删除 agent 时清理指派人、负责人）在库里的形式：不是 12 位十六进制，不会与 agent id 相同。
const SYSTEM: &str = "system";

/// 做一件事的人：用户、系统自己的清理（删除 agent），或一个 agent。库里存成 `user`、`system` 或 agent id（只经
/// [`Self::as_str`] / [`Self::from_stored`]）；JSON 按种类打标签。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Actor {
    User,
    System,
    Agent { agent_id: AgentId },
}

impl Actor {
    pub fn as_str(&self) -> &str {
        match self {
            Actor::User => USER,
            Actor::System => SYSTEM,
            Actor::Agent { agent_id } => agent_id.as_str(),
        }
    }

    /// 库里存的形式（[`Self::as_str`]）读回来。
    pub fn from_stored(s: String) -> Self {
        match s.as_str() {
            USER => Actor::User,
            SYSTEM => Actor::System,
            _ => Actor::Agent { agent_id: AgentId::new(s) },
        }
    }

    pub fn agent(&self) -> Option<&AgentId> {
        match self {
            Actor::Agent { agent_id } => Some(agent_id),
            Actor::User | Actor::System => None,
        }
    }
}

/// 一次修改是谁、在哪个会话里做的：agent 用工具时所在的会话；用户在界面上的操作、系统的清理没有会话。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct By {
    pub actor: Actor,
    pub session_id: Option<SessionId>,
}

impl By {
    pub fn user() -> Self {
        By { actor: Actor::User, session_id: None }
    }

    pub fn system() -> Self {
        By { actor: Actor::System, session_id: None }
    }

    pub fn agent(agent_id: &AgentId, session_id: &SessionId) -> Self {
        By { actor: Actor::Agent { agent_id: agent_id.clone() }, session_id: Some(session_id.clone()) }
    }
}

/// 人与 agent 协作的一件事（读模型，也是 HTTP 返回的形状）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
pub struct Issue {
    pub id: i64,
    pub title: String,
    pub description: String,
    pub status: Status,
    pub priority: Priority,
    /// 负责的项目（agent id）：创建时必填，不随指派变化，只能显式修改；只在负责的 agent 被删除后为空
    pub owner: Option<AgentId>,
    /// agent id、`user`，或 null（未指派）
    pub assignee: Option<Assignee>,
    /// 创建者：agent id；用户创建的为 `user`
    pub creator: Actor,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

pub(crate) const COLUMNS: &str =
    "id, title, description, status, priority, owner_agent_id, assignee, creator, created_at, updated_at";

pub(crate) fn issue(r: &Row<'_>) -> rusqlite::Result<Issue> {
    Ok(Issue {
        id: r.get(0)?,
        title: r.get(1)?,
        description: r.get(2)?,
        status: r.get(3)?,
        priority: r.get(4)?,
        owner: r.get(5)?,
        assignee: r.get::<_, Option<String>>(6)?.map(Assignee::from_stored),
        creator: Actor::from_stored(r.get::<_, String>(7)?),
        created_at: r.get(8)?,
        updated_at: r.get(9)?,
    })
}

/// 按条件查 issue（filter 是 WHERE 之后的部分，可带 ORDER BY）。
pub(crate) fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Issue>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM issues WHERE {filter}"))?;
    Ok(stmt.query_map(params, issue)?.collect::<rusqlite::Result<Vec<_>>>()?)
}

pub(crate) fn find_in(c: &Connection, issue_id: i64) -> Result<Option<Issue>> {
    Ok(c.query_row(&format!("SELECT {COLUMNS} FROM issues WHERE id = ?1"), [issue_id], issue).optional()?)
}

/// 存在的 issue；不存在 NotFound。
pub fn issue_in(c: &Connection, issue_id: i64) -> Result<Issue> {
    find_in(c, issue_id)?.ok_or_else(|| not_found(format!("issue #{issue_id} not found")))
}

/// 新建一行（校验在调用方），返回它的编号。
pub(crate) fn insert_in(c: &Connection, new: &Issue) -> Result<i64> {
    c.execute(
        "INSERT INTO issues (title, description, status, priority, owner_agent_id, assignee, creator, created_at, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        params![
            new.title,
            new.description,
            new.status.as_str(),
            new.priority.as_str(),
            new.owner,
            new.assignee.as_ref().map(Assignee::as_str),
            new.creator.as_str(),
            new.created_at,
            new.updated_at
        ],
    )?;
    Ok(c.last_insert_rowid())
}

/// 把改过的字段写回（只由变更单元调用）。
pub(crate) fn save_in(c: &Connection, i: &Issue) -> Result<()> {
    c.execute(
        "UPDATE issues SET title = ?2, description = ?3, status = ?4, priority = ?5, owner_agent_id = ?6, assignee = ?7,
         updated_at = ?8 WHERE id = ?1",
        params![
            i.id,
            i.title,
            i.description,
            i.status.as_str(),
            i.priority.as_str(),
            i.owner,
            i.assignee.as_ref().map(Assignee::as_str),
            i.updated_at
        ],
    )?;
    Ok(())
}

pub(crate) fn delete_in(c: &Connection, issue_id: i64) -> Result<()> {
    c.execute("DELETE FROM issues WHERE id = ?1", [issue_id])?;
    Ok(())
}
