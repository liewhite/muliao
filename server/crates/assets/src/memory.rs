//! 记忆：一块有长度上限的 markdown 文件。agent 能用（use）的每一块，每次运行全文放进指令；能改（write）的才能改。三类
//! 记忆只是放的位置与分发不同：全局记忆（`platform/memory/global.md`，base 固定收录，不能删）、共享记忆（平台目录里别的，
//! 由角色给 `*:memory:<名>`）、私有记忆（`<agent 名>/memory.md`，只它自己与 admin 改，规则不按名字指它）。

use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::{DateTime, Utc};

use crate::access::{Actor, grants_for, usable};
use crate::error::{Result, not_found};
use crate::formats::memory::text_of;
use crate::kinds::StoredKind;
use crate::source::AssetSource;
use crate::tree::{Asset, Owner, check_version, version_of};

/// 一次运行放进指令的一块记忆。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemoryBlock {
    pub name: String,
    pub content: String,
    /// 这个 agent 能改它
    pub editable: bool,
    /// 归这个 agent（它的私有记忆）
    pub yours: bool,
    pub path: PathBuf,
}

#[derive(Debug, Clone)]
pub struct MemoryInfo {
    pub name: String,
    pub owner: Owner,
    pub content: String,
    pub updated_at: DateTime<Utc>,
    pub path: PathBuf,
    /// content 的版本：整体改写时交回
    pub version: String,
}

fn info(a: &Asset) -> MemoryInfo {
    let text = a.content.memory().text.clone();
    MemoryInfo {
        name: a.name.clone(),
        owner: a.owner.clone(),
        version: version_of(&text),
        content: text,
        updated_at: a.updated_at,
        path: a.path.clone(),
    }
}

pub struct MemoryStore {
    source: Arc<AssetSource>,
}

impl MemoryStore {
    pub fn new(source: Arc<AssetSource>) -> Self {
        Self { source }
    }

    /// 每一块各自的上限。
    pub fn max_chars(&self) -> usize {
        self.source.memory_max_chars()
    }

    /// 平台的一块记忆（全局或共享）。
    pub async fn read(&self, name: &str) -> Result<MemoryInfo> {
        Ok(info(self.source.snapshot().await?.asset(StoredKind::Memory, name)?))
    }

    pub async fn read_private(&self, agent_id: &str) -> Result<MemoryInfo> {
        let snapshot = self.source.snapshot().await?;
        match snapshot.private_memory(agent_id) {
            Some(a) => Ok(info(a)),
            None => Err(not_found(format!(
                "{} has no private memory",
                snapshot.owner_label(&Owner::Agent(agent_id.to_owned()))
            ))),
        }
    }

    /// 组装一次运行的指令时读：这个 agent 能用的每一块（自己的与规则分来的），按名字。
    pub async fn for_run(&self, agent_id: &str) -> Result<Vec<MemoryBlock>> {
        let snapshot = self.source.snapshot().await?;
        let grants = grants_for(&snapshot, &Actor::Agent(agent_id.to_owned()));
        Ok(usable(&snapshot, StoredKind::Memory, &grants)
            .into_iter()
            .map(|a| MemoryBlock {
                name: a.name.clone(),
                content: a.content.memory().text.clone(),
                editable: grants.writes(a),
                yours: a.owner.is(agent_id),
                path: a.path.clone(),
            })
            .collect())
    }

    /// 新建或整体改写平台的一块记忆（用户在界面上编辑）。version 是打开编辑时的版本（新建为 None），期间被别处改过就
    /// Conflict。名字的规则（slug、不与别的重名）由写入口校验。
    pub async fn put(&self, name: &str, content: &str, version: Option<&str>) -> Result<MemoryInfo> {
        let snapshot = self.source.snapshot().await?;
        let path = match snapshot.existing(StoredKind::Memory, name)? {
            Some(a) => a.path.clone(),
            None => snapshot.path_of(StoredKind::Memory, name, &Owner::Platform)?,
        };
        self.write(&path, content, version, &format!("memory {name:?}")).await?;
        self.read(name).await
    }

    /// 整体改写 agent 的私有记忆（没有时新建），同 put。
    pub async fn put_private(&self, agent_id: &str, content: &str, version: Option<&str>) -> Result<MemoryInfo> {
        let snapshot = self.source.snapshot().await?;
        let path = snapshot.private_memory_path(agent_id)?;
        let what = format!("the private memory of {}", snapshot.owner_label(&Owner::Agent(agent_id.to_owned())));
        self.write(&path, content, version, &what).await?;
        self.read_private(agent_id).await
    }

    async fn write(&self, path: &Path, content: &str, version: Option<&str>, what: &str) -> Result<()> {
        // 此刻文件里的（写入口在锁里、紧挨着写之前调用）：期间被删、被抢先建、被改过都对不上
        let unchanged = |p: &Path| {
            let current = if p.is_file() { Some(text_of(p).map_err(crate::error::conflict)?) } else { None };
            check_version(current.as_deref(), version, what)
        };
        self.source.write(&[(path.to_path_buf(), content.to_owned())], Some(&unchanged)).await?;
        Ok(())
    }
}
