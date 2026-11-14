//! agent 与角色：列出、新建、改、默认、归档、删除；分发规则的预览；agent 能用的资源与它的私有记忆。界面上的写入都以用户的
//! 身份进行。

use axum::extract::State;
use axum::http::StatusCode;
use chrono::{DateTime, Utc};
use muliao_assets::access::{Actor, Reach, Source, UsableAsset};
use muliao_assets::distribution::{RoleSpec, RoleView, UsedAsset};
use muliao_assets::formats::agent_file::{AgentConfig, Grouping, Thinking};
use muliao_assets::formats::mcp::McpTransport;
use muliao_assets::kinds::Shown;
use muliao_assets::rules::{AgentRules, Rule, Rules, ToolSpec, texts};
use muliao_assets::service::{AgentPatch, AgentSpec, AgentView};
use muliao_platform::config::ModelRef;
use muliao_platform::errors::DomainError;
use muliao_platform::ids::AgentId;
use muliao_platform::tooling::ToolAccess;
use serde::{Deserialize, Serialize};
use utoipa::ToSchema;
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::api::assets::{MemoryIn, MemoryOut};
use crate::error::{ApiResult, ErrorOut};
use crate::extract::{Json, Path};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new()
        .routes(routes!(agent_defaults))
        .routes(routes!(preview_agent))
        .routes(routes!(list_roles))
        .routes(routes!(preview_role))
        .routes(routes!(create_role, update_role, delete_role))
        .routes(routes!(list_agents, create_agent))
        .routes(routes!(get_agent, update_agent, delete_agent))
        .routes(routes!(agent_assets))
        .routes(routes!(set_default_agent, unset_default_agent))
        .routes(routes!(archive_agent, unarchive_agent))
        .routes(routes!(get_private_memory, put_private_memory))
}

/// 一个模型：哪个 provider（服务端配置的 `[llm.providers]`）的哪个模型；agent.toml 里写作 `<provider>:<model>`。
#[derive(Serialize, Deserialize, ToSchema)]
pub struct ModelIo {
    pub provider: String,
    pub model: String,
}

impl ModelIo {
    fn of(m: &ModelRef) -> Self {
        ModelIo { provider: m.provider().to_owned(), model: m.model().to_owned() }
    }

    fn parse(self) -> ApiResult<ModelRef> {
        Ok(ModelRef::new(&self.provider, &self.model).map_err(DomainError::invalid)?)
    }
}

/// agent.toml 里的配置。
#[derive(Serialize, ToSchema)]
pub struct AgentConfigOut {
    pub description: String,
    pub model: ModelIo,
    pub thinking: Thinking,
    /// 分组名，null 为不分组
    pub group: Option<String>,
}

#[derive(Serialize, ToSchema)]
pub struct AgentOut {
    pub id: AgentId,
    pub name: String,
    /// null：它的目录没加载（agent.toml 坏了等，见资源问题），不能运行，修好文件或删掉它
    pub config: Option<AgentConfigOut>,
    pub roles: Vec<String>,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
    pub created_at: DateTime<Utc>,
    /// 系统默认 agent：Web 新对话默认选中它，最多一个
    pub is_default: bool,
    /// 归档时间；非空时不能对话、委派、指派，它的触发器不触发
    pub archived_at: Option<DateTime<Utc>>,
}

impl AgentOut {
    fn of(v: AgentView) -> Self {
        let AgentView { info, config, rules } = v;
        let (archived_at, is_default) = (info.archived_at(), info.is_default());
        AgentOut {
            id: info.id,
            name: info.name.to_string(),
            config: config.map(|d| AgentConfigOut {
                description: d.file.description().to_owned(),
                model: ModelIo::of(d.file.model()),
                thinking: d.file.thinking(),
                group: d.file.group().map(str::to_owned),
            }),
            roles: rules.roles,
            allow: texts(&rules.allow),
            deny: texts(&rules.deny),
            created_at: info.created_at,
            is_default,
            archived_at,
        }
    }
}

/// 操作之后的 agent：只取它的 id，配置与分发规则重新从快照读。
async fn out(s: &AppState, agent_id: &AgentId) -> ApiResult<AgentOut> {
    Ok(AgentOut::of(s.services.agents.view(agent_id).await?))
}

/// 新建 agent 时表单预填的值。
#[derive(Serialize, ToSchema)]
pub struct AgentDefaults {
    pub name: String,
    pub description: String,
    pub model: ModelIo,
    pub roles: Vec<String>,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
    pub thinking: Thinking,
    /// 分组名；null 为不分组
    pub group: Option<String>,
}

/// 新建 agent 时表单预填的值：创建时套用的同一份缺省（AgentSpec 的缺省，模型取服务端配置），前端不另写一份。
#[utoipa::path(get, path = "/api/agent-defaults", tag = "agents", responses((status = 200, body = AgentDefaults)))]
async fn agent_defaults(State(s): State<AppState>) -> axum::Json<AgentDefaults> {
    let spec = AgentSpec::default();
    axum::Json(AgentDefaults {
        name: spec.name,
        description: spec.description,
        model: ModelIo::of(s.services.agents.default_model()),
        roles: spec.rules.roles,
        allow: texts(&spec.rules.allow),
        deny: texts(&spec.rules.deny),
        thinking: spec.thinking,
        group: spec.group.name().map(str::to_owned),
    })
}

/// 一个内置工具（当前配置下注册的，工具自己声明读写与是否管理类），在规则里写作 `tool:<name>`。
#[derive(Serialize, ToSchema)]
pub struct ToolOut {
    pub name: String,
    pub access: Access,
    /// 管理类工具：base 角色没有
    pub admin: bool,
}

#[derive(Serialize, ToSchema)]
#[serde(rename_all = "lowercase")]
pub enum Access {
    Read,
    Write,
}

impl ToolOut {
    pub fn of(t: &ToolSpec) -> Self {
        ToolOut { name: t.name.clone(), access: Access::of(t.access), admin: t.admin }
    }
}

impl Access {
    pub fn of(a: ToolAccess) -> Self {
        match a {
            ToolAccess::Read => Access::Read,
            ToolAccess::Write => Access::Write,
        }
    }
}

/// 一份配置能用（use）的内置工具与资产（skill、MCP server、secret、记忆）。
#[derive(Serialize, ToSchema)]
pub struct ReachOut {
    pub tools: Vec<ToolOut>,
    pub assets: Vec<UsableAsset>,
}

impl ReachOut {
    fn of(r: Reach) -> Self {
        ReachOut { tools: r.tools.iter().map(ToolOut::of).collect(), assets: r.assets }
    }
}

#[derive(Deserialize, ToSchema)]
pub struct AgentPreviewIn {
    /// 已有的 agent（它自己的资产总能用）；null 为还没建的
    pub agent_id: Option<AgentId>,
    pub roles: Vec<String>,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
}

/// agent 配置（还没保存的也行）最终能用的：自己的资产 + base + 角色 + allow / deny 放行的，与运行时同一套计算，与保存时
/// 同样校验（不合法 422）。
#[utoipa::path(post, path = "/api/agent-preview", tag = "agents", request_body = AgentPreviewIn,
    responses((status = 200, body = ReachOut), (status = 422, body = ErrorOut)))]
async fn preview_agent(State(s): State<AppState>, Json(body): Json<AgentPreviewIn>) -> ApiResult<axum::Json<ReachOut>> {
    let rules =
        AgentRules { roles: body.roles, allow: Rule::parse_all(&body.allow)?, deny: Rule::parse_all(&body.deny)? };
    let reach = s.services.distribution.preview_agent(body.agent_id.as_ref(), &rules).await?;
    Ok(axum::Json(ReachOut::of(reach)))
}

#[derive(Serialize, ToSchema)]
pub struct RoleOut {
    pub name: String,
    pub description: String,
    /// 内置角色（base、admin）代码里固定的规则，不可改
    pub fixed_allow: Vec<String>,
    /// 可编辑的规则
    pub allow: Vec<String>,
    pub deny: Vec<String>,
    /// 内置角色不能删
    pub builtin: bool,
    /// 能写进 agent 的 roles：base 每个 agent 隐含，不能选
    pub assignable: bool,
    /// 只按这个角色的规则（固定的与可编辑的）能用的
    pub reach: ReachOut,
    /// 带这个角色的 agent 的 id（含归档的）；base 是全部 agent
    pub agents: Vec<AgentId>,
}

impl RoleOut {
    fn of(v: RoleView) -> Self {
        let RoleView { role, reach, holders } = v;
        RoleOut {
            builtin: role.builtin(),
            assignable: role.assignable(),
            name: role.name,
            description: role.description,
            fixed_allow: texts(&role.fixed.allow),
            allow: texts(&role.rules.allow),
            deny: texts(&role.rules.deny),
            reach: ReachOut::of(reach),
            agents: holders,
        }
    }
}

/// 全部角色：内置在前，自建的按名称。
#[utoipa::path(get, path = "/api/roles", tag = "roles", responses((status = 200, body = Vec<RoleOut>)))]
async fn list_roles(State(s): State<AppState>) -> ApiResult<axum::Json<Vec<RoleOut>>> {
    Ok(axum::Json(s.services.distribution.views().await?.into_iter().map(RoleOut::of).collect()))
}

#[derive(Deserialize, ToSchema)]
pub struct RolePreviewIn {
    /// 已有的角色（内置的加上固定规则）；null 为还没建的
    pub name: Option<String>,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
}

/// 角色规则（还没保存的也行）能用的，与保存时同样校验（不合法 422）。
#[utoipa::path(post, path = "/api/role-preview", tag = "roles", request_body = RolePreviewIn,
    responses((status = 200, body = ReachOut), (status = 422, body = ErrorOut)))]
async fn preview_role(State(s): State<AppState>, Json(body): Json<RolePreviewIn>) -> ApiResult<axum::Json<ReachOut>> {
    let reach = s
        .services
        .distribution
        .preview_role(body.name.as_deref(), &Rules::new(Rule::parse_all(&body.allow)?, Rule::parse_all(&body.deny)?))
        .await?;
    Ok(axum::Json(ReachOut::of(reach)))
}

#[derive(Deserialize, ToSchema)]
pub struct RoleIn {
    pub description: String,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
}

#[utoipa::path(post, path = "/api/roles/{name}", tag = "roles", request_body = RoleIn,
    responses((status = 201, body = RoleOut), (status = 409, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn create_role(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(body): Json<RoleIn>,
) -> ApiResult<(StatusCode, axum::Json<RoleOut>)> {
    let d = &s.services.distribution;
    d.create(&RoleSpec {
        name: name.clone(),
        description: body.description,
        rules: Rules::new(Rule::parse_all(&body.allow)?, Rule::parse_all(&body.deny)?),
    })
    .await?;
    Ok((StatusCode::CREATED, axum::Json(RoleOut::of(d.view(&name).await?))))
}

/// 只改给出的字段（缺省或 null 是不变）：界面只提交用户改过的，编辑期间 agent 改了别的字段不会被旧值盖掉。
#[derive(Deserialize, ToSchema)]
pub struct RoleUpdate {
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub allow: Option<Vec<String>>,
    #[serde(default)]
    pub deny: Option<Vec<String>>,
}

/// 改角色的描述与可编辑的规则（内置角色也可以，固定的部分不变）；从带它的 agent 的下一次运行起生效。
#[utoipa::path(patch, path = "/api/roles/{name}", tag = "roles", request_body = RoleUpdate,
    responses((status = 200, body = RoleOut), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn update_role(
    State(s): State<AppState>,
    Path(name): Path<String>,
    Json(body): Json<RoleUpdate>,
) -> ApiResult<axum::Json<RoleOut>> {
    let d = &s.services.distribution;
    let allow = body.allow.as_deref().map(Rule::parse_all).transpose()?;
    let deny = body.deny.as_deref().map(Rule::parse_all).transpose()?;
    d.patch(&name, body.description, allow, deny).await?;
    Ok(axum::Json(RoleOut::of(d.view(&name).await?)))
}

/// 有 agent 引用时 409。
#[utoipa::path(delete, path = "/api/roles/{name}", tag = "roles",
    responses((status = 204), (status = 409, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn delete_role(State(s): State<AppState>, Path(name): Path<String>) -> ApiResult<StatusCode> {
    s.services.distribution.delete(&name).await?;
    Ok(StatusCode::NO_CONTENT)
}

#[utoipa::path(get, path = "/api/agents", tag = "agents", responses((status = 200, body = Vec<AgentOut>)))]
async fn list_agents(State(s): State<AppState>) -> ApiResult<axum::Json<Vec<AgentOut>>> {
    Ok(axum::Json(s.services.agents.list_views().await?.into_iter().map(AgentOut::of).collect()))
}

#[derive(Deserialize, ToSchema)]
pub struct AgentCreate {
    pub name: String,
    pub description: String,
    /// 缺省（null）时使用服务端配置的默认值
    #[serde(default)]
    pub model: Option<ModelIo>,
    /// 角色，不含每个 agent 隐含的 base
    #[serde(default)]
    pub roles: Vec<String>,
    #[serde(default)]
    pub allow: Vec<String>,
    #[serde(default)]
    pub deny: Vec<String>,
    /// 思考档位："default" 不指定，按模型与服务端的默认
    #[serde(default)]
    pub thinking: Thinking,
    /// 分组名；null 或空串为不分组
    #[serde(default)]
    pub group: Option<String>,
}

#[utoipa::path(post, path = "/api/agents", tag = "agents", request_body = AgentCreate,
    responses((status = 201, body = AgentOut), (status = 409, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn create_agent(
    State(s): State<AppState>,
    Json(body): Json<AgentCreate>,
) -> ApiResult<(StatusCode, axum::Json<AgentOut>)> {
    let spec = AgentSpec {
        name: body.name,
        description: body.description,
        model: body.model.map(ModelIo::parse).transpose()?,
        thinking: body.thinking,
        group: body.group.as_deref().map_or(Grouping::Ungrouped, Grouping::parse),
        rules: AgentRules {
            roles: body.roles,
            allow: Rule::parse_all(&body.allow)?,
            deny: Rule::parse_all(&body.deny)?,
        },
    };
    let created = s.services.agents.create(spec, &Actor::User).await?;
    Ok((StatusCode::CREATED, axum::Json(out(&s, &created.id).await?)))
}

/// 正在删除的按不存在处理。
#[utoipa::path(get, path = "/api/agents/{agent_id}", tag = "agents",
    responses((status = 200, body = AgentOut), (status = 404, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn get_agent(State(s): State<AppState>, Path(agent_id): Path<AgentId>) -> ApiResult<axum::Json<AgentOut>> {
    Ok(axum::Json(out(&s, &agent_id).await?))
}

/// 只改给出的字段（缺省或 null 是不变）：界面只提交用户改过的，编辑期间 agent 自己改了另一个字段不会被旧值盖掉。
#[derive(Deserialize, ToSchema)]
pub struct AgentUpdate {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub model: Option<ModelIo>,
    #[serde(default)]
    pub roles: Option<Vec<String>>,
    #[serde(default)]
    pub allow: Option<Vec<String>>,
    #[serde(default)]
    pub deny: Option<Vec<String>>,
    #[serde(default)]
    pub thinking: Option<Thinking>,
    /// 空串为不分组
    #[serde(default)]
    pub group: Option<String>,
}

#[utoipa::path(patch, path = "/api/agents/{agent_id}", tag = "agents", request_body = AgentUpdate,
    responses((status = 200, body = AgentOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 422, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn update_agent(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
    Json(body): Json<AgentUpdate>,
) -> ApiResult<axum::Json<AgentOut>> {
    let patch = AgentPatch {
        name: body.name,
        config: AgentConfig {
            description: body.description,
            model: body.model.map(ModelIo::parse).transpose()?,
            thinking: body.thinking,
            group: body.group.as_deref().map(Grouping::parse),
        },
        roles: body.roles,
        allow: body.allow.as_deref().map(Rule::parse_all).transpose()?,
        deny: body.deny.as_deref().map(Rule::parse_all).transpose()?,
    };
    let updated = s.services.agents.update(&agent_id, patch).await?;
    Ok(axum::Json(out(&s, &updated.id).await?))
}

/// 删除 agent：它的会话、触发器、指派等各领域的数据一起清理，资源文件里别人还在用的转给平台。
#[utoipa::path(delete, path = "/api/agents/{agent_id}", tag = "agents",
    responses((status = 204), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn delete_agent(State(s): State<AppState>, Path(agent_id): Path<AgentId>) -> ApiResult<StatusCode> {
    s.services.lifecycle.delete(&agent_id, &Actor::User).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// 为什么能用：它名下的、放行它的角色（含 base）、它自己的 allow 规则（至少一项）。
#[derive(Serialize, ToSchema)]
pub struct AssetSourceOut {
    pub own: bool,
    pub roles: Vec<String>,
    pub allow: bool,
}

impl AssetSourceOut {
    fn of(s: Source) -> Self {
        AssetSourceOut { own: s.own, roles: s.roles, allow: s.allow }
    }
}

#[derive(Serialize, ToSchema)]
pub struct AssetOut {
    pub name: String,
    /// null 归平台
    pub owner: Option<AgentId>,
    pub description: String,
    pub source: AssetSourceOut,
    /// 它能改（write）
    pub editable: bool,
}

#[derive(Serialize, ToSchema)]
pub struct McpAssetOut {
    pub name: String,
    pub owner: Option<AgentId>,
    pub description: String,
    pub source: AssetSourceOut,
    pub editable: bool,
    pub transport: McpTransport,
    pub enabled: bool,
}

/// agent 能用的资产，按种类分组，各组按名称。
#[derive(Serialize, ToSchema)]
pub struct AgentAssetsOut {
    pub memories: Vec<AssetOut>,
    /// 记忆注入 instructions 的那一段（与运行时同一个渲染函数）
    pub memory_preview: String,
    pub skills: Vec<AssetOut>,
    pub secrets: Vec<AssetOut>,
    pub mcp: Vec<McpAssetOut>,
}

fn asset_out(u: UsedAsset) -> AssetOut {
    AssetOut {
        name: u.name,
        owner: u.owner.agent().cloned(),
        description: u.description,
        source: AssetSourceOut::of(u.source),
        editable: u.editable,
    }
}

/// agent 能用的记忆、skill、secret、MCP server 与各自的来源（不含 secret 的值），加上记忆合并后放进指令的样子。
#[utoipa::path(get, path = "/api/agents/{agent_id}/assets", tag = "agents",
    responses((status = 200, body = AgentAssetsOut), (status = 404, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn agent_assets(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
) -> ApiResult<axum::Json<AgentAssetsOut>> {
    let used = s.services.distribution.assets(&agent_id).await?;
    let memory = &s.services.memory;
    let preview = muliao_runtime::instructions::memory_section(&memory.for_agent(&agent_id).await?, memory.max_chars());
    let mut out =
        AgentAssetsOut { memories: vec![], memory_preview: preview, skills: vec![], secrets: vec![], mcp: vec![] };
    for u in used {
        match &u.content {
            Shown::Memory(_) => out.memories.push(asset_out(u)),
            Shown::Skill(_) => out.skills.push(asset_out(u)),
            Shown::Secret { .. } => out.secrets.push(asset_out(u)),
            Shown::Mcp(file) => {
                let (transport, enabled) = (file.config.transport(), file.enabled);
                let a = asset_out(u);
                out.mcp.push(McpAssetOut {
                    name: a.name,
                    owner: a.owner,
                    description: a.description,
                    source: a.source,
                    editable: a.editable,
                    transport,
                    enabled,
                });
            }
        }
    }
    Ok(axum::Json(out))
}

/// 设为系统默认 agent（取代原来的默认）。
#[utoipa::path(put, path = "/api/agents/{agent_id}/default", tag = "agents",
    responses((status = 200, body = AgentOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn set_default_agent(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
) -> ApiResult<axum::Json<AgentOut>> {
    let a = s.services.agents.set_default(&agent_id, true).await?;
    Ok(axum::Json(out(&s, &a.id).await?))
}

/// 取消它的系统默认身份；它不是默认时不做任何事。
#[utoipa::path(delete, path = "/api/agents/{agent_id}/default", tag = "agents",
    responses((status = 200, body = AgentOut), (status = 404, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn unset_default_agent(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
) -> ApiResult<axum::Json<AgentOut>> {
    let a = s.services.agents.set_default(&agent_id, false).await?;
    Ok(axum::Json(out(&s, &a.id).await?))
}

/// 归档：不能再对话、委派、指派，它的触发器不触发；它忙或是默认 agent 时 409。
#[utoipa::path(put, path = "/api/agents/{agent_id}/archive", tag = "agents",
    responses((status = 200, body = AgentOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn archive_agent(State(s): State<AppState>, Path(agent_id): Path<AgentId>) -> ApiResult<axum::Json<AgentOut>> {
    let a = s.services.lifecycle.set_archived(&agent_id, true).await?;
    Ok(axum::Json(out(&s, &a.id).await?))
}

/// 取消归档；没归档时不做任何事。
#[utoipa::path(delete, path = "/api/agents/{agent_id}/archive", tag = "agents",
    responses((status = 200, body = AgentOut), (status = 404, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn unarchive_agent(State(s): State<AppState>, Path(agent_id): Path<AgentId>) -> ApiResult<axum::Json<AgentOut>> {
    let a = s.services.lifecycle.set_archived(&agent_id, false).await?;
    Ok(axum::Json(out(&s, &a.id).await?))
}

/// agent 的私有记忆。
#[utoipa::path(get, path = "/api/agents/{agent_id}/memory", tag = "agents",
    responses((status = 200, body = MemoryOut), (status = 404, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn get_private_memory(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
) -> ApiResult<axum::Json<MemoryOut>> {
    let m = &s.services.memory;
    Ok(axum::Json(MemoryOut::of(m.read_private(&agent_id).await?, m.max_chars())))
}

/// 整体改写 agent 的私有记忆（没有时新建）。只有它自己用；规则不按名字指它。
#[utoipa::path(put, path = "/api/agents/{agent_id}/memory", tag = "agents", request_body = MemoryIn,
    responses((status = 200, body = MemoryOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 422, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号")))]
async fn put_private_memory(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
    Json(body): Json<MemoryIn>,
) -> ApiResult<axum::Json<MemoryOut>> {
    let m = &s.services.memory;
    let info = m.put_private(&agent_id, &body.content, body.version.as_deref()).await?;
    Ok(axum::Json(MemoryOut::of(info, m.max_chars())))
}
