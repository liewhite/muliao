//! 资源池（[`AssetCatalog`]）：跨种类看资源——全部资源与收录它们的角色（界面的资源页）——与各种类通用的写：删除、转移
//! owner（把文件或目录挪到新 owner 的目录下）。

use std::path::PathBuf;
use std::sync::Arc;

use chrono::{DateTime, Utc};

use crate::access::Sharing;
use crate::formats::mcp::McpFile;
use crate::formats::memory::MemoryText;
use crate::formats::skill::SkillDir;
use crate::kinds::{Content, Kind, StoredKind};
use crate::rules::{GLOBAL_MEMORY, ToolSpec};
use crate::source::AssetSource;
use crate::tree::{Owner, remove};
use muliao_platform::errors::{Result, conflict, invalid};

/// 种类专有的详情：内置工具的读写与是否管理类；存成文件的是它的内容，secret 只有 allow_shell（密文不出这个领域）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Detail {
    Tool(ToolSpec),
    Memory(MemoryText),
    Skill(SkillDir),
    Mcp(McpFile),
    Secret { allow_shell: bool },
}

/// 资源池里的一项。内置工具归平台、没有描述、修改时间与文件。
#[derive(Debug, Clone)]
pub struct CatalogAsset {
    pub kind: Kind,
    pub name: String,
    pub owner: Owner,
    pub description: String,
    pub updated_at: Option<DateTime<Utc>>,
    /// 收录它的角色（含内置的）
    pub roles: Vec<String>,
    /// 分发出去了：否则只有 owner 能用（归平台的就没有 agent 能用）
    pub shared: bool,
    /// 文件或目录的位置；内置工具为 None
    pub path: Option<PathBuf>,
    pub detail: Detail,
}

pub struct AssetCatalog {
    source: Arc<AssetSource>,
}

impl AssetCatalog {
    pub fn new(source: Arc<AssetSource>) -> Self {
        Self { source }
    }

    /// 全部资源：内置工具（按注册顺序）在前，skill、MCP server、secret、记忆按种类、名称；每项带收录它的角色。
    pub async fn catalog(&self) -> Result<Vec<CatalogAsset>> {
        let snapshot = self.source.snapshot().await?;
        let sharing = Sharing::of(&snapshot);
        let tools = snapshot.tools.specs().iter().map(|t| CatalogAsset {
            kind: Kind::Tool,
            name: t.name.clone(),
            owner: Owner::Platform,
            description: String::new(),
            updated_at: None,
            roles: sharing.including(Kind::Tool, Some(&t.name)),
            shared: sharing.shared(Kind::Tool, Some(&t.name), &Owner::Platform),
            path: None,
            detail: Detail::Tool(t.clone()),
        });
        let files = StoredKind::BY_NAME.into_iter().flat_map(|k| snapshot.of_kind(k)).map(|a| CatalogAsset {
            kind: a.kind.kind(),
            name: a.name.clone(),
            owner: a.owner.clone(),
            description: a.description().to_owned(),
            updated_at: Some(a.updated_at),
            roles: sharing.including(a.kind.kind(), a.rule_name()),
            shared: sharing.shared(a.kind.kind(), a.rule_name(), &a.owner),
            path: Some(a.path.clone()),
            detail: match &a.content {
                Content::Memory(m) => Detail::Memory(m.clone()),
                Content::Skill(s) => Detail::Skill(s.clone()),
                Content::Mcp(m) => Detail::Mcp(m.clone()),
                Content::Secret(s) => Detail::Secret { allow_shell: s.allow_shell },
            },
        });
        Ok(tools.chain(files).collect())
    }

    /// 删掉这项按名字认的资源的文件或目录（用户在界面上删；私有记忆随 agent）。全局记忆不能删（base 固定收录它）。收录
    /// 它的规则留着：同名的再建时分发不丢。
    pub async fn delete(&self, kind: StoredKind, name: &str) -> Result<()> {
        if kind == StoredKind::Memory && name == GLOBAL_MEMORY {
            return Err(invalid(format!("the global memory {GLOBAL_MEMORY:?} cannot be deleted")));
        }
        let w = self.source.writing().await?;
        remove(&w.snapshot.asset(kind, name)?.path)?;
        tracing::info!(kind = %kind, asset = name, "asset deleted");
        Ok(())
    }

    /// 把资源挪到 owner 的目录下（用户在界面上转）。内容不变；谁能用随 owner 变。记忆不转移：平台的记忆归了 agent 就是第二
    /// 块私有记忆（每个 agent 只有一块）；要共享就把内容放进一块平台记忆。
    pub async fn transfer(&self, kind: StoredKind, name: &str, owner: &Owner) -> Result<()> {
        if kind == StoredKind::Memory {
            return Err(invalid(
                "memories are not transferred: each agent has exactly one private memory, which belongs to it; to \
                 share content, put it into a platform memory",
            ));
        }
        let w = self.source.writing().await?;
        let snapshot = &w.snapshot;
        let asset = snapshot.asset(kind, name)?;
        if asset.owner == *owner {
            return Err(invalid(format!(
                "{} {name:?} already belongs to {}",
                kind.label(),
                snapshot.owner_label(owner)
            )));
        }
        // 目标 owner 不存在、正在删除或目录没加载时 NotFound
        let target = snapshot.path_of(kind, name, owner)?;
        let copies = snapshot.shadowed(kind, name);
        if matches!(owner, Owner::Agent(_)) && !copies.is_empty() {
            // 平台那份遮住着 agent 目录里的同名副本（#502）：转给 agent 后就是 agent 之间同名，两边都不加载
            return Err(conflict(format!(
                "{} {name:?} has copies in agent directories that the platform's one hides: {copies:?}; once it belongs \
                 to an agent, the copies would clash with it and none would load. Rename or remove the copies first",
                kind.label()
            )));
        }
        crate::tree::move_to(&asset.path, &target)?;
        tracing::info!(kind = %kind, asset = name, owner = ?owner, "asset transferred");
        Ok(())
    }
}
