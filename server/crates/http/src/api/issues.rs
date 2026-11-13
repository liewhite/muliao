//! issue 看板：界面上的操作都以用户的身份进行。返回的 issue 与时间线就是看板领域的读模型（BoardIssue、Activity）。

use axum::extract::State;
use axum::http::StatusCode;
use muliao_issues::activity::Activity;
use muliao_issues::graph::Upstream;
use muliao_issues::{Assignee, BoardIssue, By, Filter, IssueDetail, NewIssue, Patch, Priority, Status};
use muliao_platform::ids::AgentId;
use serde::Deserialize;
use utoipa::{IntoParams, ToSchema};
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::error::{ApiResult, ErrorOut};
use crate::extract::{Json, Path, Query};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new()
        .routes(routes!(list_issues, create_issue))
        .routes(routes!(get_issue, update_issue, delete_issue))
        .routes(routes!(comment_issue))
        .routes(routes!(approve_issue))
        .routes(routes!(start_issue))
        .routes(routes!(reject_issue))
}

#[derive(Deserialize, IntoParams)]
#[into_params(parameter_in = Query)]
pub struct IssueQuery {
    /// 只要这些状态（可以重复给）；不给是全部
    #[serde(default)]
    #[param(value_type = Option<Vec<Status>>)]
    pub status: Vec<Status>,
    /// 指派人：agent id，或 `user` 表示用户
    pub assignee: Option<String>,
    /// 负责项目的 agent id
    pub owner: Option<AgentId>,
}

#[utoipa::path(get, path = "/api/issues", tag = "issues", params(IssueQuery),
    responses((status = 200, body = Vec<BoardIssue>), (status = 422, body = ErrorOut)))]
async fn list_issues(State(s): State<AppState>, Query(q): Query<IssueQuery>) -> ApiResult<axum::Json<Vec<BoardIssue>>> {
    // 查询参数沿用库里的写法（`user` 或 agent id）：查询串里放不了按种类打标签的对象
    let filter = Filter { statuses: q.status, assignee: q.assignee.map(Assignee::from_stored), owner: q.owner };
    Ok(axum::Json(s.services.board.list(filter).await?))
}

#[derive(Deserialize, ToSchema)]
pub struct IssueCreate {
    pub title: String,
    /// 负责的项目（agent id），必填
    pub owner: AgentId,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub status: Status,
    #[serde(default)]
    pub priority: Priority,
    /// 指派人；null 不指派
    #[serde(default)]
    pub assignee: Option<Assignee>,
    /// 依赖的上游 issue id；不能成环
    #[serde(default)]
    pub depends_on: Vec<i64>,
}

#[utoipa::path(post, path = "/api/issues", tag = "issues", request_body = IssueCreate,
    responses((status = 201, body = BoardIssue), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn create_issue(
    State(s): State<AppState>,
    Json(body): Json<IssueCreate>,
) -> ApiResult<(StatusCode, axum::Json<BoardIssue>)> {
    let board = &s.services.board;
    let new = NewIssue {
        owner: body.owner,
        title: body.title,
        description: body.description,
        status: body.status,
        priority: body.priority,
        assignee: body.assignee,
        depends_on: body.depends_on.into_iter().map(Upstream::Issue).collect(),
        key: None,
    };
    let created = board.create_many(By::user(), vec![new]).await?;
    let id = created.first().expect("one issue was created").id;
    Ok((StatusCode::CREATED, axum::Json(board.get(id).await?)))
}

#[utoipa::path(get, path = "/api/issues/{issue_id}", tag = "issues",
    responses((status = 200, body = IssueDetail), (status = 404, body = ErrorOut)))]
async fn get_issue(State(s): State<AppState>, Path(issue_id): Path<i64>) -> ApiResult<axum::Json<IssueDetail>> {
    Ok(axum::Json(s.services.board.detail(issue_id).await?))
}

/// 只修改给出的字段。assignee 给出 null 表示取消指派，不出现表示不变。
#[derive(Deserialize, ToSchema)]
pub struct IssuePatch {
    #[serde(default)]
    pub owner: Option<AgentId>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub status: Option<Status>,
    #[serde(default)]
    pub priority: Option<Priority>,
    /// 指派人；null 取消指派，不出现不变
    #[serde(default, deserialize_with = "crate::extract::present")]
    #[schema(value_type = Option<Assignee>)]
    pub assignee: Option<Option<Assignee>>,
    /// 依赖的上游 issue id，整体替换（[] 去掉全部）；不出现表示不变
    #[serde(default)]
    pub depends_on: Option<Vec<i64>>,
}

#[utoipa::path(patch, path = "/api/issues/{issue_id}", tag = "issues", request_body = IssuePatch,
    responses((status = 200, body = BoardIssue), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 422, body = ErrorOut)))]
async fn update_issue(
    State(s): State<AppState>,
    Path(issue_id): Path<i64>,
    Json(body): Json<IssuePatch>,
) -> ApiResult<axum::Json<BoardIssue>> {
    let patch = Patch {
        owner: body.owner,
        title: body.title,
        description: body.description,
        status: body.status,
        priority: body.priority,
        assignee: body.assignee,
        depends_on: body.depends_on,
    };
    let board = &s.services.board;
    let issue = board.update(By::user(), issue_id, patch).await?;
    Ok(axum::Json(board.get(issue.id).await?))
}

#[derive(Deserialize, ToSchema)]
pub struct CommentIn {
    pub body: String,
}

#[utoipa::path(post, path = "/api/issues/{issue_id}/comments", tag = "issues", request_body = CommentIn,
    responses((status = 201, body = Activity), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn comment_issue(
    State(s): State<AppState>,
    Path(issue_id): Path<i64>,
    Json(body): Json<CommentIn>,
) -> ApiResult<(StatusCode, axum::Json<Activity>)> {
    Ok((StatusCode::CREATED, axum::Json(s.services.board.comment(By::user(), issue_id, body.body).await?)))
}

/// 审核通过指派给用户的 issue：设为 done。没有指派给用户时 409。
#[utoipa::path(post, path = "/api/issues/{issue_id}/approve", tag = "issues",
    responses((status = 200, body = BoardIssue), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)))]
async fn approve_issue(State(s): State<AppState>, Path(issue_id): Path<i64>) -> ApiResult<axum::Json<BoardIssue>> {
    let board = &s.services.board;
    let issue = board.approve(issue_id).await?;
    Ok(axum::Json(board.get(issue.id).await?))
}

/// 开工 backlog 里的 issue：指派给 owner 并进入 in_progress。不在 backlog 或没有 owner 时 409。
#[utoipa::path(post, path = "/api/issues/{issue_id}/start", tag = "issues",
    responses((status = 200, body = BoardIssue), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)))]
async fn start_issue(State(s): State<AppState>, Path(issue_id): Path<i64>) -> ApiResult<axum::Json<BoardIssue>> {
    let board = &s.services.board;
    let issue = board.start(issue_id).await?;
    Ok(axum::Json(board.get(issue.id).await?))
}

#[derive(Deserialize, ToSchema)]
pub struct RejectIn {
    /// 打回的理由，记成评论
    pub reason: String,
}

/// 打回指派给用户的 issue：理由记成评论，回到 in_progress，指派给把它交给用户的 agent。没有指派给用户、或不是 agent 交来的
/// 时 409。
#[utoipa::path(post, path = "/api/issues/{issue_id}/reject", tag = "issues", request_body = RejectIn,
    responses((status = 200, body = BoardIssue), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)))]
async fn reject_issue(
    State(s): State<AppState>,
    Path(issue_id): Path<i64>,
    Json(body): Json<RejectIn>,
) -> ApiResult<axum::Json<BoardIssue>> {
    let board = &s.services.board;
    let issue = board.reject(issue_id, body.reason).await?;
    Ok(axum::Json(board.get(issue.id).await?))
}

#[utoipa::path(delete, path = "/api/issues/{issue_id}", tag = "issues",
    responses((status = 204), (status = 404, body = ErrorOut)))]
async fn delete_issue(State(s): State<AppState>, Path(issue_id): Path<i64>) -> ApiResult<StatusCode> {
    s.services.board.delete(issue_id).await?;
    Ok(StatusCode::NO_CONTENT)
}
