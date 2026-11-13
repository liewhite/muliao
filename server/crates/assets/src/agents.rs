//! agents 表：agent 的身份（id、名字）、默认、归档、删除标记。别的领域只经这里的 `*_in` 查询（在自己的事务里），不碰表。
//!
//! - 正在删除的按不存在处理（docs/architecture.md 第 10 节）：这条规则只在 [`LIVE`] 一处，按 id 取行、按名称找、列出都
//!   用它；归档的另外不能运行（[`runnable_agent_in`]）。
//! - agent 只会从可用变为正在删除、再变为不存在。为某个 agent 写数据库之前，在写入的同一事务里检查它还可用
//!   （[`active_agent_in`]）：事务串行，检查通过的写入一定落在删除标记之前，被删除流程的清理一起删掉。它的资源文件由
//!   资源快照拦：快照带着正在删除的名单，往它目录里写一律拒绝。

use muliao_platform::ids::AgentId;
use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Utc};
use muliao_platform::db::{Database, Migration};
use rusqlite::{Connection, OptionalExtension, Row, params};

use crate::naming::AgentName;
use muliao_platform::errors::{Error, Result, conflict, not_found};

pub const MIGRATION: Migration = Migration {
    domain: "agents",
    steps: &[
        "
        CREATE TABLE agents (
            -- 系统生成、不可变：主键、目录名（工作目录）、URL 与工具调用都用它
            id TEXT PRIMARY KEY NOT NULL,
            -- 名字只认这里（也是资源目录里它的目录名）；唯一以免委派时混淆
            name TEXT NOT NULL UNIQUE,
            -- 正在删除时是发起删除的人：不出现在列表与名称解析里，删完文件后删除这一行
            deleting_by TEXT,
            -- 系统默认 agent（Web 新对话默认选中它），最多一个
            is_default INTEGER NOT NULL DEFAULT 0,
            -- 归档时间：非空时不能运行，取消归档后恢复
            archived_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX uq_agents_default ON agents (is_default) WHERE is_default;
    ",
        "
        -- 名字的比较键（AgentName::key，大小写折叠）：只差大小写的名字是同一个（资源目录在不分大小写的文件系统上）。
        -- 旧行用 lower()：它只折叠 ASCII，上线时已有的名字都是小写 ASCII 与中文（两者折叠结果相同，11-13 核对）
        ALTER TABLE agents ADD COLUMN name_key TEXT NOT NULL DEFAULT '';
        UPDATE agents SET name_key = lower(name);
        CREATE UNIQUE INDEX uq_agents_name_key ON agents (name_key);
    ",
        "
        -- 正在删除（#803 起只是一个标记，原 deleting_by 存的发起人没人读，日志里有）：不出现在列表与名称解析里，删完文件后
        -- 删除这一行
        ALTER TABLE agents ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0;
        UPDATE agents SET deleting = 1 WHERE deleting_by IS NOT NULL;
        ALTER TABLE agents DROP COLUMN deleting_by;
    ",
    ],
};

/// 还可用（不在删除中）的 agent
const LIVE: &str = "NOT deleting";
const COLUMNS: &str = "id, name, is_default, archived_at, created_at, deleting";

/// 别的领域看到的 agent 身份（读模型）。配置（描述、模型、思考档位、分组）不在这里，在资源快照。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentInfo {
    pub id: AgentId,
    pub name: AgentName,
    pub created_at: DateTime<Utc>,
    /// 系统默认 agent，最多一个
    pub is_default: bool,
    pub state: AgentState,
}

/// agent 处在哪个阶段。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentState {
    Active,
    /// 已归档：不能运行，取消归档后恢复
    Archived {
        at: DateTime<Utc>,
    },
    /// 正在删除（只有 [`agent_in`] 会给出这样的）
    Deleting,
}

impl AgentInfo {
    pub fn archived(&self) -> bool {
        matches!(self.state, AgentState::Archived { .. })
    }

    /// 归档时间；没归档为 None。
    pub fn archived_at(&self) -> Option<DateTime<Utc>> {
        match self.state {
            AgentState::Archived { at } => Some(at),
            _ => None,
        }
    }
}

fn info(row: &Row<'_>) -> rusqlite::Result<AgentInfo> {
    let archived_at: Option<DateTime<Utc>> = row.get(3)?;
    let deleting: bool = row.get(5)?;
    let state = match (deleting, archived_at) {
        (true, _) => AgentState::Deleting,
        (false, Some(at)) => AgentState::Archived { at },
        (false, None) => AgentState::Active,
    };
    Ok(AgentInfo { id: row.get(0)?, name: row.get(1)?, is_default: row.get(2)?, created_at: row.get(4)?, state })
}

fn one(c: &Connection, filter: &str, value: &dyn rusqlite::ToSql) -> Result<Option<AgentInfo>> {
    let sql = format!("SELECT {COLUMNS} FROM agents WHERE {filter}");
    Ok(c.query_row(&sql, [value], info).optional()?)
}

/// agent（包括正在删除的）：展示信息（名称）；不存在 NotFound。
pub fn agent_in(c: &Connection, agent_id: &AgentId) -> Result<AgentInfo> {
    one(c, "id = ?1", agent_id)?.ok_or_else(|| not_found(format!("agent {agent_id:?} not found")))
}

/// 还可用的 agent（正在删除的按不存在处理）。别的领域在自己的写事务里为某个 agent 写数据之前用它检查。管理与资源的写入
/// （改配置、记忆、secret）用它，归档的照常可改。
pub fn active_agent_in(c: &Connection, agent_id: &AgentId) -> Result<AgentInfo> {
    one(c, &format!("id = ?1 AND {LIVE}"), agent_id)?.ok_or_else(|| not_found(format!("agent {agent_id:?} not found")))
}

/// agent 现在能不能接新工作（不报错的查询，给要按情况分别处理的投递方）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Availability {
    Runnable,
    /// 已归档：取消归档后恢复
    Archived,
    /// 不存在或正在删除
    Gone,
}

/// 与 [`runnable_agent_in`] 同一条规则，按结果返回而不报错。
pub fn availability_in(c: &Connection, agent_id: &AgentId) -> Result<Availability> {
    Ok(match one(c, &format!("id = ?1 AND {LIVE}"), agent_id)? {
        None => Availability::Gone,
        Some(a) if a.archived() => Availability::Archived,
        Some(_) => Availability::Runnable,
    })
}

/// 能运行的 agent：还可用且没有归档，归档的报 Conflict。给它派新工作的入口用它（来信与新会话、指派与 owner、触发器的
/// 创建与触发）。
pub fn runnable_agent_in(c: &Connection, agent_id: &AgentId) -> Result<AgentInfo> {
    let agent = active_agent_in(c, agent_id)?;
    if agent.archived() {
        return Err(conflict(format!("agent {:?} is archived; unarchive it first", agent.name)));
    }
    Ok(agent)
}

/// 按名称找还可用的 agent（名称唯一），没有时 None。
pub fn named_or_none_in(c: &Connection, name: &AgentName) -> Result<Option<AgentInfo>> {
    one(c, &format!("name_key = ?1 AND {LIVE}"), &name.key())
}

/// 按名称找还可用的 agent（模型按名称指代 agent，不接触 id）。名称不合规则时 InvalidInput；没有时 NotFound，列出可用的
/// 名称。
pub fn named_in(c: &Connection, name: &str) -> Result<AgentInfo> {
    if let Some(a) = named_or_none_in(c, &AgentName::parse(name)?)? {
        return Ok(a);
    }
    let names: Vec<String> = listed_in(c)?.into_iter().map(|a| a.name.to_string()).collect();
    Err(not_found(format!("no agent named {name:?}; available agents: {names:?}")))
}

/// 列表里的 agent（正在删除的不列出），按名称。
pub fn listed_in(c: &Connection) -> Result<Vec<AgentInfo>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM agents WHERE {LIVE} ORDER BY name_key"))?;
    let rows = stmt.query_map([], info)?.collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

/// agent id → 展示名称（包括正在删除的）。别的领域里留着的 id 可能属于已删除的 agent，显示为「(deleted agent <id>)」。
#[derive(Debug, Clone, Default)]
pub struct AgentNames(pub BTreeMap<AgentId, String>);

impl AgentNames {
    pub fn name(&self, agent_id: &AgentId) -> String {
        self.0.get(agent_id).cloned().unwrap_or_else(|| format!("(deleted agent {agent_id})"))
    }
}

pub fn agent_names_in(c: &Connection) -> Result<AgentNames> {
    let mut stmt = c.prepare("SELECT id, name FROM agents")?;
    let names = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
    Ok(AgentNames(names))
}

/// 读资源树时按目录名对上 agent 的依据：全部 agent（id → 名字，含正在删除的）与其中正在删除的。一条语句读出，两者一致。
pub(crate) async fn identities(db: &Database) -> Result<(BTreeMap<AgentId, AgentName>, BTreeSet<AgentId>)> {
    db.read(|c| {
        let mut stmt = c.prepare("SELECT id, name, deleting FROM agents")?;
        let mut known = BTreeMap::new();
        let mut deleting = BTreeSet::new();
        for row in
            stmt.query_map([], |r| Ok((r.get::<_, AgentId>(0)?, r.get::<_, AgentName>(1)?, r.get::<_, bool>(2)?)))?
        {
            let (id, name, gone) = row?;
            if gone {
                deleting.insert(id.clone());
            }
            known.insert(id, name);
        }
        Ok::<_, Error>((known, deleting))
    })
    .await
}

// ---- agent 领域自己的写入（服务与删除、归档流程用） ----

pub(crate) fn insert_in(c: &Connection, agent_id: &AgentId, name: &AgentName) -> Result<AgentInfo> {
    let now = Utc::now();
    c.execute(
        "INSERT INTO agents (id, name, name_key, is_default, created_at, updated_at) VALUES (?1, ?2, ?3, 0, ?4, ?4)",
        params![agent_id, name.as_str(), name.key(), now],
    )?;
    agent_in(c, agent_id)
}

pub(crate) fn rename_in(c: &Connection, agent_id: &AgentId, name: &AgentName) -> Result<AgentInfo> {
    active_agent_in(c, agent_id)?;
    c.execute(
        "UPDATE agents SET name = ?2, name_key = ?3, updated_at = ?4 WHERE id = ?1",
        params![agent_id, name.as_str(), name.key(), Utc::now()],
    )?;
    agent_in(c, agent_id)
}

/// 名字被数据库里别的 agent（含目录坏掉、没加载的、正在删除的）占着时 Conflict（只差大小写也算）。
pub(crate) fn check_name_free_in(c: &Connection, name: &AgentName, agent_id: Option<&AgentId>) -> Result<()> {
    let holder: Option<AgentId> =
        c.query_row("SELECT id FROM agents WHERE name_key = ?1", [name.key()], |r| r.get(0)).optional()?;
    match holder {
        Some(h) if Some(&h) != agent_id => {
            Err(conflict(format!("agent name {name:?} is already taken by another agent")))
        }
        _ => Ok(()),
    }
}

/// 可以开始删除的 agent：存在且不在删除中（否则 NotFound / Conflict）。
pub(crate) fn deletable_in(c: &Connection, agent_id: &AgentId) -> Result<AgentInfo> {
    let agent = agent_in(c, agent_id)?;
    if agent.state == AgentState::Deleting {
        return Err(conflict(format!("agent {:?} is already being deleted", agent.name)));
    }
    Ok(agent)
}

pub(crate) fn mark_deleting_in(c: &Connection, agent_id: &AgentId) -> Result<()> {
    deletable_in(c, agent_id)?;
    c.execute(
        "UPDATE agents SET deleting = 1, is_default = 0, updated_at = ?2 WHERE id = ?1",
        params![agent_id, Utc::now()],
    )?;
    Ok(())
}

/// 正在删除的 agent 的 id，按 id。
pub(crate) fn deleting_in(c: &Connection) -> Result<Vec<AgentId>> {
    let mut stmt = c.prepare("SELECT id FROM agents WHERE deleting ORDER BY id")?;
    let rows = stmt.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

/// 正在删除的这个 agent；不存在或没在删除时 None。
pub(crate) fn being_deleted_in(c: &Connection, agent_id: &AgentId) -> Result<Option<AgentInfo>> {
    one(c, "id = ?1 AND deleting", agent_id)
}

/// 删除正在删除的 agent 的行（它的文件已删）；已不在时什么都不做（删除重做）。
pub(crate) fn remove_in(c: &Connection, agent_id: &AgentId) -> Result<bool> {
    Ok(c.execute("DELETE FROM agents WHERE id = ?1 AND deleting", [agent_id])? > 0)
}

pub(crate) fn check_archivable(agent: &AgentInfo) -> Result<()> {
    if agent.is_default {
        return Err(conflict(format!(
            "agent {:?} is the default agent; unset it as default first, then archive",
            agent.name
        )));
    }
    Ok(())
}

/// 归档或取消归档还可用的 agent；已是目标状态时不改。归档要求它不是系统默认 agent。
pub(crate) fn set_archived_in(c: &Connection, agent_id: &AgentId, archived: bool) -> Result<AgentInfo> {
    let agent = active_agent_in(c, agent_id)?;
    if agent.archived() != archived {
        if archived {
            check_archivable(&agent)?;
        }
        let at = archived.then(Utc::now);
        c.execute(
            "UPDATE agents SET archived_at = ?2, updated_at = ?3 WHERE id = ?1",
            params![agent_id, at, Utc::now()],
        )?;
    }
    agent_in(c, agent_id)
}

/// 设为（或取消）系统默认 agent。设为默认时先清掉旧的，始终最多一个；归档的不能设为默认（新对话会默认选中一个不能
/// 运行的 agent）。
pub(crate) fn set_default_in(c: &Connection, agent_id: &AgentId, is_default: bool) -> Result<AgentInfo> {
    if is_default {
        runnable_agent_in(c, agent_id)?;
        c.execute("UPDATE agents SET is_default = 0 WHERE is_default AND id != ?1", [agent_id])?;
    } else {
        active_agent_in(c, agent_id)?;
    }
    c.execute(
        "UPDATE agents SET is_default = ?2, updated_at = ?3 WHERE id = ?1",
        params![agent_id, is_default, Utc::now()],
    )?;
    agent_in(c, agent_id)
}
