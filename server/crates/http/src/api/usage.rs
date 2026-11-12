//! 用量：按时间范围汇总 token 用量。

use axum::extract::State;
use chrono::{DateTime, FixedOffset, Utc};
use muliao_usage::{UsageBucket, UsageGroup};
use serde::Deserialize;
use utoipa::IntoParams;
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::error::{ApiResult, ErrorOut};
use crate::extract::Query;
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new().routes(routes!(usage_summary))
}

#[derive(Deserialize, IntoParams)]
#[into_params(parameter_in = Query)]
pub struct UsageQuery {
    /// 带时区的时刻（`2026-03-01T00:00:00Z`）：不带时区的时刻没有确定含义，拒绝
    #[param(value_type = String, format = DateTime)]
    pub since: DateTime<FixedOffset>,
    #[param(value_type = String, format = DateTime)]
    pub until: DateTime<FixedOffset>,
    /// 分组维度（可以重复给）；不给就只有一行总计
    #[serde(default)]
    #[param(inline)]
    pub group_by: Vec<UsageGroup>,
}

/// [since, until) 之间的用量，按给出的维度分组。「天」按服务端配置的时区划分日历日，与客户端所在时区无关。input_tokens 是
/// 输入总数，已包含 cache_read_tokens 与 cache_write_tokens，展示时是总数与其中两部分的关系，不要相加。没有参与分组的维度
/// 为 null。
#[utoipa::path(get, path = "/api/usage", tag = "usage", params(UsageQuery),
    responses((status = 200, body = Vec<UsageBucket>), (status = 422, body = ErrorOut)))]
async fn usage_summary(
    State(s): State<AppState>,
    Query(q): Query<UsageQuery>,
) -> ApiResult<axum::Json<Vec<UsageBucket>>> {
    let (since, until) = (q.since.with_timezone(&Utc), q.until.with_timezone(&Utc));
    Ok(axum::Json(s.services.usage.summarize(since, until, &q.group_by).await?))
}
