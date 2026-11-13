//! 工作历史：界面在 agent 页列出它的历史（只读；写入只经 agent 的 history_append）。

use axum::extract::State;
use chrono::NaiveDate;
use muliao_history::{Record, Search};
use muliao_platform::ids::{AgentId, Importance};
use serde::Deserialize;
use utoipa::IntoParams;
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::error::{ApiResult, ErrorOut};
use crate::extract::{Path, Query};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new().routes(routes!(agent_history))
}

#[derive(Deserialize, IntoParams)]
#[into_params(parameter_in = Query)]
pub struct HistoryQuery {
    /// 关键词（每个词都要出现在标题、摘要或引用里）；不给按日期倒序
    pub query: Option<String>,
    /// 含两端的日期
    pub since: Option<NaiveDate>,
    pub until: Option<NaiveDate>,
    /// 1–3，默认 1
    pub min_importance: Option<Importance>,
    /// 条数，不给取默认；超出上限 422
    pub limit: Option<usize>,
}

/// agent 的工作历史：没给 query 按日期倒序；给了按关键词检索（规则同工具 history_search）。since / until 含两端。
#[utoipa::path(get, path = "/api/agents/{agent_id}/history", tag = "history", params(("agent_id" = AgentId, Path, description = "agent 的编号"), HistoryQuery),
    responses((status = 200, body = Vec<Record>), (status = 404, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn agent_history(
    State(s): State<AppState>,
    Path(agent_id): Path<AgentId>,
    Query(q): Query<HistoryQuery>,
) -> ApiResult<axum::Json<Vec<Record>>> {
    // 正在删除的 agent 按不存在处理
    s.services.agents.get_active(&agent_id).await?;
    let mut search = Search::of(Some(vec![agent_id]));
    search.query = q.query;
    search.since = q.since;
    search.until = q.until;
    if let Some(m) = q.min_importance {
        search.min_importance = m;
    }
    if let Some(l) = q.limit {
        search.limit = l;
    }
    Ok(axum::Json(s.services.history.search(search).await?))
}
