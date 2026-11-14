//! AI 资源（内置工具、skill、MCP server、secret、记忆）：`/assets` 列出全部资源（每种带自己的详情），`/assets/<种类>/{name}`
//! 读、写、删一个资源，`/assets/{kind}/{name}/owner` 对存成文件的资源通用（挪到另一个 owner 的目录），各种类专有的操作在
//! 自己的子路径（MCP 的启停与测试连接、skill 目录里文件的浏览）。资源都是资源目录里的文件，这里读写的就是它们。
//!
//! 这里的写入都是用户在界面上做的；新建的资源归请求给出的 owner（缺省为平台），已有的 owner 不变。

use muliao_platform::ids::AgentId;
use std::collections::BTreeMap;

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::Response;
use chrono::{DateTime, Utc};
use muliao_assets::access::Actor;
use muliao_assets::catalog::{CatalogAsset, Detail};
use muliao_assets::formats::mcp::{McpConfig, McpTransport};
use muliao_assets::kinds::{Shown, StoredKind};
use muliao_assets::mcp::McpServer;
use muliao_assets::memory::MemoryInfo;
use muliao_assets::tree::Owner;
use muliao_platform::errors::DomainError;
use serde::{Deserialize, Serialize};
use utoipa::ToSchema;
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::api::agents::Access;
use crate::api::workspace::{EntryOut, PathQuery, browse_file};
use crate::error::{ApiResult, ErrorOut};
use crate::extract::{Json, Path, Query};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new()
        .routes(routes!(list_assets))
        .routes(routes!(rule_candidates))
        .routes(routes!(transfer_asset))
        .routes(routes!(get_memory, put_memory, delete_memory))
        .routes(routes!(get_skill, put_skill, delete_skill))
        .routes(routes!(list_skill_files))
        .routes(routes!(read_skill_file))
        .routes(routes!(get_mcp, add_mcp, delete_mcp))
        .routes(routes!(set_mcp_enabled))
        .routes(routes!(probe_mcp))
        .routes(routes!(get_secret, set_secret, delete_secret))
}

pub fn owner_of(owner: Option<AgentId>) -> Owner {
    owner.map_or(Owner::Platform, Owner::Agent)
}

fn owner_out(owner: &Owner) -> Option<AgentId> {
    owner.agent().cloned()
}

// ---- 资源池 ----

/// 资源池里的一项，在规则里写作 `<action>:<kind>:<name>`；按 kind 区分的详情。
#[derive(Serialize, ToSchema)]
pub struct CatalogAssetOut {
    pub name: String,
    /// agent 的 id；null 归平台（内置工具、界面上建的、迁移前的公共资产）
    pub owner: Option<AgentId>,
    /// 收录它的角色（allow 放行、deny 没拦，含内置的 base、admin）
    pub roles: Vec<String>,
    /// 分发出去了：base 收录它，或某个不是 owner 的 agent 的规则（角色或自己的 allow）放行它；否则只有 owner 能用（归平台的
    /// 就谁也用不了）。与删除 agent 时把资产转给平台同一个定义
    pub shared: bool,
    /// 用户能在界面上删它（内置工具、全局记忆、私有记忆不能）
    pub deletable: bool,
    /// 用户能在界面上把它转给别的 owner（内置工具、记忆不能）
    pub transferable: bool,
    #[serde(flatten)]
    pub detail: CatalogDetail,
}

/// 存成文件的一项的文件：描述、修改时间与位置（skill 是目录）。
#[derive(Serialize, ToSchema)]
pub struct StoredFileOut {
    pub description: String,
    pub updated_at: DateTime<Utc>,
    pub path: String,
}

#[derive(Serialize, ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CatalogDetail {
    /// 内置工具：归平台、代码里定义，没有文件，不能改
    Tool {
        access: Access,
        /// 管理类工具：base 角色没有
        admin: bool,
    },
    Skill {
        file: StoredFileOut,
    },
    Mcp {
        file: StoredFileOut,
        enabled: bool,
        /// env / headers 里的 secret 只以 ${vault:KEY} 引用形式存储，可以原样返回
        config: McpConfig,
    },
    /// 值不返回
    Secret {
        file: StoredFileOut,
        /// false：agent 自己写的命令不能注入，只有 skill_run 的 skill 脚本与 MCP 配置能用（#528）
        allow_shell: bool,
    },
    /// 正文不在列表里：平台的记忆（owner 为 null）用 GET /api/assets/memory/{name} 读，agent 的私有记忆（owner 是它，name 是
    /// 它的名字，只作显示）用 GET /api/agents/{owner}/memory
    Memory {
        file: StoredFileOut,
    },
}

fn catalog_out(e: CatalogAsset) -> CatalogAssetOut {
    let detail = match e.detail {
        Detail::Tool(t) => CatalogDetail::Tool { access: Access::of(t.access), admin: t.admin },
        Detail::Stored { file, shown } => {
            let file = StoredFileOut {
                description: file.description,
                updated_at: file.updated_at,
                path: file.path.display().to_string(),
            };
            match shown {
                Shown::Skill(_) => CatalogDetail::Skill { file },
                Shown::Mcp(m) => CatalogDetail::Mcp { file, enabled: m.enabled, config: m.config },
                Shown::Secret { allow_shell } => CatalogDetail::Secret { file, allow_shell },
                Shown::Memory(_) => CatalogDetail::Memory { file },
            }
        }
    };
    CatalogAssetOut {
        owner: owner_out(&e.owner),
        name: e.name,
        roles: e.roles,
        shared: e.shared,
        deletable: e.deletable,
        transferable: e.transferable,
        detail,
    }
}

/// 全部资源：内置工具（当前配置下注册的，按注册顺序）在前，存成文件的按种类、名称；每种带自己的详情（工具的读写与管理类、
/// MCP 的配置与启停）。
#[utoipa::path(get, path = "/api/assets", tag = "assets", responses((status = 200, body = Vec<CatalogAssetOut>)))]
async fn list_assets(State(s): State<AppState>) -> ApiResult<axum::Json<Vec<CatalogAssetOut>>> {
    Ok(axum::Json(s.services.catalog.catalog().await?.into_iter().map(catalog_out).collect()))
}

/// 填分发规则时的补全候选：每种的 `use:kind:*`，然后每项资源的 `use:kind:name`，能写的种类再加 `*:kind:name`。
#[utoipa::path(get, path = "/api/assets/rule-candidates", tag = "assets", responses((status = 200, body = Vec<String>)))]
async fn rule_candidates(State(s): State<AppState>) -> ApiResult<axum::Json<Vec<String>>> {
    Ok(axum::Json(s.services.catalog.rule_candidates().await?))
}

// ---- 存成文件的资源通用：删除、owner ----

/// 删掉存成文件的一项资源（skill 是整个目录）。每种在自己的路径上（同一路径上已有这种资源的读写）：记忆只指平台的（私有
/// 记忆随 agent），全局记忆不能删（422）；内置工具不能删。
async fn delete_stored(s: &AppState, kind: StoredKind, name: &str) -> ApiResult<StatusCode> {
    s.services.catalog.delete(kind, name).await?;
    Ok(StatusCode::NO_CONTENT)
}

#[utoipa::path(delete, path = "/api/assets/memory/{name}", tag = "assets",
    responses((status = 204), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn delete_memory(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<StatusCode> {
    delete_stored(&s, StoredKind::Memory, &name).await
}

#[utoipa::path(delete, path = "/api/assets/skill/{name}", tag = "assets",
    responses((status = 204), (status = 404, body = ErrorOut)))]
async fn delete_skill(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<StatusCode> {
    delete_stored(&s, StoredKind::Skill, &name).await
}

#[utoipa::path(delete, path = "/api/assets/mcp/{name}", tag = "assets",
    responses((status = 204), (status = 404, body = ErrorOut)))]
async fn delete_mcp(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<StatusCode> {
    delete_stored(&s, StoredKind::Mcp, &name).await
}

#[utoipa::path(delete, path = "/api/assets/secret/{name}", tag = "assets",
    responses((status = 204), (status = 404, body = ErrorOut)))]
async fn delete_secret(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<StatusCode> {
    delete_stored(&s, StoredKind::Secret, &name).await
}

#[derive(Deserialize, ToSchema)]
pub struct OwnerIn {
    /// agent 的 id；null 归平台
    pub owner: Option<AgentId>,
}

/// 挪到另一个 owner 的目录下；内容与收录它的角色不变。记忆不转移（422）；内置工具归平台，不在这里（kind 不接受 tool）。
#[utoipa::path(put, path = "/api/assets/{kind}/{name}/owner", tag = "assets", request_body = OwnerIn,
    params(("kind" = StoredKind, Path, description = "skill、mcp、secret 或 memory")),
    responses((status = 204), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn transfer_asset(
    State(s): State<AppState>,
    Path((kind, name)): Path<(StoredKind, String)>,
    Json(body): Json<OwnerIn>,
) -> ApiResult<StatusCode> {
    s.services.catalog.transfer(kind, &name, &owner_of(body.owner)).await?;
    Ok(StatusCode::NO_CONTENT)
}

// ---- 记忆 ----

#[derive(Serialize, ToSchema)]
pub struct MemoryOut {
    pub name: String,
    pub owner: Option<AgentId>,
    pub content: String,
    pub updated_at: DateTime<Utc>,
    pub path: String,
    /// 每一块的上限
    pub max_chars: usize,
    /// content 的版本：保存时交回
    pub version: String,
}

impl MemoryOut {
    pub fn of(m: MemoryInfo, max_chars: usize) -> Self {
        MemoryOut {
            owner: owner_out(&m.owner),
            name: m.name,
            content: m.content,
            updated_at: m.updated_at,
            path: m.path.display().to_string(),
            max_chars,
            version: m.version,
        }
    }
}

#[derive(Deserialize, ToSchema)]
pub struct MemoryIn {
    pub content: String,
    /// 打开编辑时的版本（MemoryOut.version），新建为 null；期间被别处改过、删掉或抢先建了时 409
    pub version: Option<String>,
}

/// 平台的一块记忆（全局或共享）。
#[utoipa::path(get, path = "/api/assets/memory/{name}", tag = "assets",
    responses((status = 200, body = MemoryOut), (status = 404, body = ErrorOut)))]
async fn get_memory(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<axum::Json<MemoryOut>> {
    let m = &s.services.memory;
    Ok(axum::Json(MemoryOut::of(m.read(&name).await?, m.max_chars())))
}

/// 新建（名字要是 slug）或整体改写平台的一块记忆。谁能用由角色收录它（`use:memory:<名>`）。
#[utoipa::path(put, path = "/api/assets/memory/{name}", tag = "assets", request_body = MemoryIn,
    responses((status = 200, body = MemoryOut), (status = 409, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn put_memory(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(body): Json<MemoryIn>,
) -> ApiResult<axum::Json<MemoryOut>> {
    let m = &s.services.memory;
    Ok(axum::Json(MemoryOut::of(m.put(&name, &body.content, body.version.as_deref()).await?, m.max_chars())))
}

// ---- skill ----

#[derive(Serialize, ToSchema)]
pub struct SkillOut {
    pub name: String,
    pub description: String,
    pub owner: Option<AgentId>,
}

#[derive(Serialize, ToSchema)]
pub struct SkillDetail {
    pub name: String,
    pub description: String,
    pub owner: Option<AgentId>,
    pub body: String,
    /// SKILL.md 的版本：保存时交回
    pub version: String,
}

#[derive(Deserialize, ToSchema)]
pub struct SkillIn {
    /// 新建时的 owner；已有的不变
    pub owner: Option<AgentId>,
    pub description: String,
    pub body: String,
    /// 打开编辑时的版本（SkillDetail.version），新建为 null；期间被别处改过、删掉或抢先建了时 409
    pub version: Option<String>,
}

#[utoipa::path(get, path = "/api/assets/skill/{name}", tag = "assets",
    responses((status = 200, body = SkillDetail), (status = 404, body = ErrorOut)))]
async fn get_skill(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<axum::Json<SkillDetail>> {
    let skill = s.services.skills.read(&name).await?;
    Ok(axum::Json(SkillDetail {
        owner: owner_out(&skill.meta.owner),
        name: skill.meta.name,
        description: skill.meta.description,
        body: skill.body,
        version: skill.version,
    }))
}

/// 新建的归 owner；已有的整体改写正文与描述（附件不变），owner 不变。
#[utoipa::path(put, path = "/api/assets/skill/{name}", tag = "assets", request_body = SkillIn,
    responses((status = 200, body = SkillOut), (status = 409, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn put_skill(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(body): Json<SkillIn>,
) -> ApiResult<axum::Json<SkillOut>> {
    let owner = owner_of(body.owner);
    let meta = s.services.skills.write(&name, &body.description, &body.body, &owner, body.version.as_deref()).await?;
    Ok(axum::Json(SkillOut { owner: owner_out(&meta.owner), name: meta.name, description: meta.description }))
}

/// skill 目录里 path（相对 skill 目录，空为根）这一层，同工作目录浏览：目录在前，各自按名称排序。
#[utoipa::path(get, path = "/api/assets/skill/{name}/files", tag = "assets", params(PathQuery),
    responses((status = 200, body = Vec<EntryOut>), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn list_skill_files(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Query(q): Query<PathQuery>,
) -> ApiResult<axum::Json<Vec<EntryOut>>> {
    let root = s.services.skills.directory(&name).await?;
    let entries = blocking(move || muliao_workspace::browse::list_dir(&root, &q.path)).await?;
    Ok(axum::Json(entries.into_iter().map(EntryOut::of).collect()))
}

/// skill 目录里一个文件的内容（同工作目录浏览：图片 inline，其余作为附件）。
#[utoipa::path(get, path = "/api/assets/skill/{name}/file", tag = "assets", params(PathQuery),
    responses((status = 200, description = "文件内容"), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn read_skill_file(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Query(q): Query<PathQuery>,
) -> ApiResult<Response> {
    let root = s.services.skills.directory(&name).await?;
    let file = blocking(move || muliao_workspace::browse::file_in(&root, &q.path)).await?;
    browse_file(file).await
}

/// 阻塞 IO（浏览目录）放到阻塞线程上做
pub async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> Result<T, DomainError> + Send + 'static,
) -> Result<T, DomainError> {
    tokio::task::spawn_blocking(f).await.expect("browsing does not panic")
}

// ---- MCP server ----

#[derive(Serialize, ToSchema)]
pub struct McpOut {
    pub owner: Option<AgentId>,
    pub name: String,
    pub enabled: bool,
    /// env / headers 里的 secret 只以 ${vault:KEY} 引用形式存储，可以原样返回
    pub config: McpConfig,
    /// 新增时连接验证列出的工具（不验证时为空）
    pub tools: Vec<String>,
}

impl McpOut {
    fn of(m: &McpServer, tools: Vec<String>) -> Self {
        McpOut { owner: owner_out(&m.owner), name: m.name.clone(), enabled: m.enabled, config: m.config.clone(), tools }
    }
}

#[derive(Deserialize, ToSchema)]
pub struct McpIn {
    pub owner: Option<AgentId>,
    pub transport: McpTransport,
    #[serde(default)]
    pub command: Option<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    /// 先连接一次验证（按用户能用的 secret 解析引用），连不上 422、不保存
    #[serde(default = "yes")]
    pub probe: bool,
}

fn yes() -> bool {
    true
}

#[utoipa::path(get, path = "/api/assets/mcp/{name}", tag = "assets",
    responses((status = 200, body = McpOut), (status = 404, body = ErrorOut)))]
async fn get_mcp(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<axum::Json<McpOut>> {
    Ok(axum::Json(McpOut::of(&s.services.mcp.get(&name).await?, vec![])))
}

/// 新增（启用的）server；同名的已存在 409（名字全局唯一，配置不能原地改写：删了再建）。
#[utoipa::path(put, path = "/api/assets/mcp/{name}", tag = "assets", request_body = McpIn,
    responses((status = 201, body = McpOut), (status = 409, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn add_mcp(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(body): Json<McpIn>,
) -> ApiResult<(StatusCode, axum::Json<McpOut>)> {
    let config = McpConfig::new(body.transport, body.command, body.args, body.url, body.env, body.headers)
        .map_err(DomainError::invalid)?;
    let connector = s.services.mcp_connector.clone();
    let probe = body.probe.then_some(async move |server: &McpServer| connector.probe(server, &Actor::User).await);
    let (server, tools) = s.services.mcp.add(&name, config, owner_of(body.owner), probe).await?;
    Ok((StatusCode::CREATED, axum::Json(McpOut::of(&server, tools))))
}

#[derive(Deserialize, ToSchema)]
pub struct EnabledIn {
    pub enabled: bool,
}

#[utoipa::path(put, path = "/api/assets/mcp/{name}/enabled", tag = "assets", request_body = EnabledIn,
    responses((status = 200, body = McpOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)))]
async fn set_mcp_enabled(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(body): Json<EnabledIn>,
) -> ApiResult<axum::Json<McpOut>> {
    Ok(axum::Json(McpOut::of(&s.services.mcp.set_enabled(&name, body.enabled).await?, vec![])))
}

/// 连接一次，列出它的工具（测试连接）。
#[utoipa::path(post, path = "/api/assets/mcp/{name}/tools", tag = "assets",
    responses((status = 200, body = Vec<String>), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn probe_mcp(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<axum::Json<Vec<String>>> {
    let server = s.services.mcp.get(&name).await?;
    Ok(axum::Json(s.services.mcp_connector.probe(&server, &Actor::User).await?))
}

// ---- secret ----

#[derive(Serialize, ToSchema)]
pub struct SecretOut {
    pub owner: Option<AgentId>,
    pub name: String,
    pub description: String,
    pub allow_shell: bool,
    pub updated_at: DateTime<Utc>,
}

#[derive(Deserialize, ToSchema)]
pub struct SecretIn {
    /// 新建时的 owner；已有的不变
    #[serde(default)]
    pub owner: Option<AgentId>,
    pub value: String,
    #[serde(default)]
    pub description: String,
    /// null：已有的保持原值，新建的允许
    #[serde(default)]
    pub allow_shell: Option<bool>,
}

/// 不含值。
#[utoipa::path(get, path = "/api/assets/secret/{name}", tag = "assets",
    responses((status = 200, body = SecretOut), (status = 404, body = ErrorOut)))]
async fn get_secret(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<axum::Json<SecretOut>> {
    let x = s.services.secrets.get(&name).await?;
    Ok(axum::Json(SecretOut {
        owner: owner_out(&x.owner),
        name: x.key,
        description: x.description,
        allow_shell: x.allow_shell,
        updated_at: x.updated_at,
    }))
}

/// 新建的归 owner（缺省为平台）；已有的改值与描述，owner 不变。
#[utoipa::path(put, path = "/api/assets/secret/{name}", tag = "assets", request_body = SecretIn,
    responses((status = 204), (status = 403, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn set_secret(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(body): Json<SecretIn>,
) -> ApiResult<StatusCode> {
    let owner = owner_of(body.owner);
    s.services.secrets.set(&name, &body.value, &body.description, &Actor::User, &owner, body.allow_shell).await?;
    Ok(StatusCode::NO_CONTENT)
}
