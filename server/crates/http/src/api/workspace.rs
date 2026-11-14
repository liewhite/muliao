//! agent 工作目录的只读浏览（agent 页「文件」：列出一层目录、读一个文件；skill 页的文件浏览在 [`crate::api::assets`]，响应
//! 形状同这里）、skill 脚本的「始终允许」（设置页：列出与撤销）、结束后台任务（会话页的等待项）。
//!
//! 文件内容是 agent 写的，与界面同源、带着登录 cookie，所以和对话里的文件下载一样按不可信处理：只有能内联的图片按 inline
//! 返回，其余一律作为附件，不在本站执行；界面预览文本用 fetch 读内容，不受 attachment 影响。内容随时会变，不缓存。

use axum::extract::State;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::Response;
use chrono::{DateTime, Utc};
use muliao_platform::ids::{AgentId, GrantId, JobId, SessionId};
use muliao_workspace::browse::{Entry, EntryKind, WorkspaceFile};
use muliao_workspace::grants::GrantInfo;
use serde::{Deserialize, Serialize};
use utoipa::{IntoParams, ToSchema};
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::api::assets::blocking;
use crate::download::{Disposition, file_response, untrusted_headers};
use crate::error::{ApiResult, ErrorOut};
use crate::extract::{Path, Query};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new()
        .routes(routes!(list_workspace))
        .routes(routes!(read_workspace_file))
        .routes(routes!(list_script_grants))
        .routes(routes!(revoke_script_grant))
        .routes(routes!(kill_job))
}

/// 目录里的一项：目录或文件，大小与能否内联只有文件有。
#[derive(Serialize, ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EntryOut {
    Dir {
        name: String,
        modified: DateTime<Utc>,
    },
    File {
        name: String,
        modified: DateTime<Utc>,
        size: u64,
        /// 能内联显示的图片；其余读文件时作为附件返回
        inline: bool,
    },
}

impl EntryOut {
    pub fn of(e: Entry) -> Self {
        let Entry { name, modified, kind } = e;
        match kind {
            EntryKind::Dir => EntryOut::Dir { name, modified },
            EntryKind::File { size, inline } => EntryOut::File { name, modified, size, inline },
        }
    }
}

#[derive(Deserialize, IntoParams)]
#[into_params(parameter_in = Query)]
pub struct PathQuery {
    /// 相对目录根的路径，空为根
    #[serde(default)]
    pub path: String,
}

/// 浏览中的一个文件（工作目录、skill 目录）：只有能内联的图片 inline，其余作为附件，按不可信内容返回，不缓存。
pub async fn browse_file(f: WorkspaceFile) -> ApiResult<Response> {
    let name = f.path.file_name().unwrap_or_default().to_string_lossy().into_owned();
    let disposition = if f.inline() { Disposition::Inline } else { Disposition::Attachment };
    let no_cache = (header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    file_response(&f.path, f.media_type.as_str(), Some((disposition, &name)), untrusted_headers().chain([no_cache]))
        .await
}

/// 列出工作目录里 path（相对工作目录，空为根）这一层：目录在前，各自按名称排序。
#[utoipa::path(get, path = "/api/agents/{agent_id}/workspace", tag = "workspace", params(("agent_id" = AgentId, Path, description = "agent 的编号"), PathQuery),
    responses((status = 200, body = Vec<EntryOut>), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn list_workspace(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
    Query(q): Query<PathQuery>,
) -> ApiResult<axum::Json<Vec<EntryOut>>> {
    s.services.agents.get_active(&agent_id).await?;
    let files = s.services.workspace_files.clone();
    let entries = blocking(move || files.list(&agent_id, &q.path)).await?;
    Ok(axum::Json(entries.into_iter().map(EntryOut::of).collect()))
}

/// 工作目录里一个文件的内容。
#[utoipa::path(get, path = "/api/agents/{agent_id}/workspace/file", tag = "workspace", params(("agent_id" = AgentId, Path, description = "agent 的编号"), PathQuery),
    responses((status = 200, description = "文件内容"), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn read_workspace_file(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
    Query(q): Query<PathQuery>,
) -> ApiResult<Response> {
    s.services.agents.get_active(&agent_id).await?;
    let files = s.services.workspace_files.clone();
    browse_file(blocking(move || files.file(&agent_id, &q.path)).await?).await
}

#[derive(Serialize, ToSchema)]
pub struct ScriptGrantOut {
    pub id: GrantId,
    pub agent_id: AgentId,
    pub skill: String,
    pub script: String,
    pub args: Vec<String>,
    pub secrets: Vec<String>,
    /// 用户点「始终允许」的那次调用所在的会话（可能已删除）
    pub session_id: SessionId,
    pub created_at: DateTime<Utc>,
    /// 按它直接放行的次数与最后一次的时间
    pub hits: i64,
    pub last_used_at: Option<DateTime<Utc>>,
}

impl ScriptGrantOut {
    fn of(g: GrantInfo) -> Self {
        ScriptGrantOut {
            id: g.id,
            agent_id: g.agent_id,
            skill: g.skill,
            script: g.script,
            args: g.args,
            secrets: g.secrets,
            session_id: g.session_id,
            created_at: g.created_at,
            hits: g.hits,
            last_used_at: g.last_used_at,
        }
    }
}

/// skill 脚本的「始终允许」（#531），新的在前。
#[utoipa::path(get, path = "/api/script-grants", tag = "workspace", responses((status = 200, body = Vec<ScriptGrantOut>)))]
async fn list_script_grants(State(s): State<AppState>) -> ApiResult<axum::Json<Vec<ScriptGrantOut>>> {
    Ok(axum::Json(s.services.grants.list().await?.into_iter().map(ScriptGrantOut::of).collect()))
}

/// 撤销一条始终允许：之后同一调用重新要批准。
#[utoipa::path(delete, path = "/api/script-grants/{grant_id}", tag = "workspace",
    params(("grant_id" = GrantId, Path, description = "始终允许的编号")),
    responses((status = 204), (status = 404, body = ErrorOut)))]
async fn revoke_script_grant(State(s): State<AppState>, Path(grant_id): Path<GrantId>) -> ApiResult<StatusCode> {
    s.services.grants.revoke(grant_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// 结束这个 agent 的一个运行中的后台任务；结局（被结束）照常作为事件回到发起它的会话。已经结束的 409。
#[utoipa::path(post, path = "/api/agents/{agent_id}/jobs/{job_id}/kill", tag = "workspace",
    responses((status = 202), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)),
    params(("agent_id" = AgentId, Path, description = "agent 的编号"), ("job_id" = JobId, Path, description = "后台任务的编号")))]
async fn kill_job(
    State(s): State<AppState>,
    Path((agent_id, job_id)): Path<(AgentId, JobId)>,
) -> ApiResult<StatusCode> {
    s.services.jobs.kill(job_id, &agent_id).await?;
    Ok(StatusCode::ACCEPTED)
}
