//! 资源池（[`AssetCatalog`]）：跨种类看资源——全部资源与收录它们的角色（界面的资源页）——与各种类通用的写：删除、转移
//! owner（把文件或目录挪到新 owner 的目录下）。

use std::path::PathBuf;
use std::sync::Arc;

use chrono::{DateTime, Utc};

use crate::access::Sharing;
use crate::kinds::{Kind, Shown, StoredKind};
use crate::roles::RoleName;
use crate::rules::{GLOBAL_MEMORY, Target, ToolSpec, rule_candidates};
use crate::source::AssetSource;
use crate::tree::{AssetKey, Owner, remove};
use muliao_platform::errors::{Result, conflict, invalid};

/// 种类专有的详情：内置工具的读写与是否管理类（代码里定义，没有文件）；存成文件的是它的文件与能给领域外看的内容（[`Shown`]）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Detail {
    Tool(ToolSpec),
    Stored { file: StoredFile, shown: Shown },
}

/// 存成文件的一项：描述、文件的修改时间与位置（skill 是目录）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredFile {
    pub description: String,
    pub updated_at: DateTime<Utc>,
    pub path: PathBuf,
}

/// 资源池里的一项。种类就是详情的种类（[`CatalogAsset::kind`]）。
#[derive(Debug, Clone)]
pub struct CatalogAsset {
    pub name: String,
    pub owner: Owner,
    /// 收录它的角色（含内置的）
    pub roles: Vec<RoleName>,
    /// 分发出去了：否则只有 owner 能用（归平台的就没有 agent 能用）
    pub shared: bool,
    /// 用户能在界面上删它（[`undeletable`]）
    pub deletable: bool,
    /// 能转给别的 owner（[`untransferable`]）
    pub transferable: bool,
    pub detail: Detail,
}

impl CatalogAsset {
    pub fn kind(&self) -> Kind {
        match &self.detail {
            Detail::Tool(_) => Kind::Tool,
            Detail::Stored { shown, .. } => shown.kind().kind(),
        }
    }
}

pub struct AssetCatalog {
    source: Arc<AssetSource>,
}

/// 用户在界面上删不了这项存成文件的资源的原因（资源池的「能不能删」和删除入口都用它）；能删为 None。
fn undeletable(kind: StoredKind, key: &AssetKey) -> Option<String> {
    match (kind, key) {
        (_, AssetKey::Private { .. }) => {
            Some("a private memory goes with its agent; it cannot be deleted on its own".into())
        }
        (StoredKind::Memory, AssetKey::Named { name, owner: Owner::Platform }) if name == GLOBAL_MEMORY => {
            Some(format!("the global memory {GLOBAL_MEMORY:?} cannot be deleted: the base role always includes it"))
        }
        (StoredKind::Memory | StoredKind::Skill | StoredKind::Mcp | StoredKind::Secret, AssetKey::Named { .. }) => None,
    }
}

/// 这一种转不了 owner 的原因（资源池的「能不能转」和转移入口都用它）；能转为 None。
fn untransferable(kind: Kind) -> Option<&'static str> {
    match kind {
        Kind::Tool => Some("built-in tools are defined in code and belong to the platform"),
        // 平台的记忆归了 agent 就是第二块私有记忆（每个 agent 只有一块）；要共享就把内容放进一块平台记忆
        Kind::Memory => Some(
            "memories are not transferred: each agent has exactly one private memory, which belongs to it; to share \
             content, put it into a platform memory",
        ),
        Kind::Skill | Kind::Mcp | Kind::Secret => None,
    }
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
            name: t.name.clone(),
            owner: Owner::Platform,
            roles: sharing.including(Target::tool(&t.name)),
            shared: sharing.shared(Target::tool(&t.name), &Owner::Platform),
            deletable: false,
            transferable: untransferable(Kind::Tool).is_none(),
            detail: Detail::Tool(t.clone()),
        });
        let files = StoredKind::BY_NAME.into_iter().flat_map(|k| snapshot.of_kind(k)).map(|a| CatalogAsset {
            name: a.name().to_owned(),
            owner: a.owner(),
            roles: sharing.including(a.target()),
            shared: sharing.shared(a.target(), &a.owner()),
            deletable: undeletable(a.kind(), &a.key).is_none(),
            transferable: untransferable(a.kind().kind()).is_none(),
            detail: Detail::Stored {
                file: StoredFile {
                    description: a.description().to_owned(),
                    updated_at: a.updated_at,
                    path: a.path.clone(),
                },
                shown: a.content.shown(),
            },
        });
        Ok(tools.chain(files).collect())
    }

    /// 删掉这项按名字认的资源的文件或目录（用户在界面上删）；删不了的见 [`undeletable`]。收录它的规则留着：同名的
    /// 再建时分发不丢。
    pub async fn delete(&self, kind: StoredKind, name: &str) -> Result<()> {
        let w = self.source.writing().await?;
        let a = w.snapshot.asset(kind, name)?;
        if let Some(reason) = undeletable(kind, &a.key) {
            return Err(invalid(reason));
        }
        remove(&a.path)?;
        tracing::info!(kind = %kind, asset = name, "asset deleted");
        Ok(())
    }

    /// 规则补全的候选（[`crate::rules::rule_candidates`]）：内置工具与按名字认的资源（私有记忆在规则里没有名字，不在内）。
    pub async fn rule_candidates(&self) -> Result<Vec<String>> {
        let snapshot = self.source.snapshot().await?;
        let tools = snapshot.tools.specs().iter().map(|t| (Kind::Tool, t.name.as_str()));
        let files = StoredKind::BY_NAME.into_iter().flat_map(|k| snapshot.named(k).map(move |a| (k.kind(), a.name())));
        Ok(rule_candidates(tools.chain(files)))
    }

    /// 把资源挪到 owner 的目录下（用户在界面上转）。内容不变；谁能用随 owner 变。转不了的见 [`untransferable`]。
    pub async fn transfer(&self, kind: StoredKind, name: &str, owner: &Owner) -> Result<()> {
        if let Some(reason) = untransferable(kind.kind()) {
            return Err(invalid(reason));
        }
        let w = self.source.writing().await?;
        let snapshot = &w.snapshot;
        let asset = snapshot.asset(kind, name)?;
        if asset.owner() == *owner {
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
