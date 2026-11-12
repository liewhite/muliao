//! 模型与服务端配置决定的界面行为：配置里的 LLM provider 与各 provider 的模型列表（界面选 agent 的模型），可选能力（显示或
//! 隐藏对应入口）。

use axum::extract::State;
use serde::Serialize;
use utoipa::ToSchema;
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::error::{ApiResult, ErrorOut};
use crate::extract::Path;
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new().routes(routes!(list_providers)).routes(routes!(list_provider_models)).routes(routes!(features))
}

/// 配置里的 LLM provider 名，即 agent 模型 "<provider 名>:<模型名>" 的前半部分。
#[utoipa::path(get, path = "/api/providers", tag = "models", responses((status = 200, body = Vec<String>)))]
async fn list_providers(State(s): State<AppState>) -> axum::Json<Vec<String>> {
    axum::Json(s.services.models.provider_names())
}

/// 向该 provider 实时查询的模型名；provider 出错时为 502 并带上原因。
#[utoipa::path(get, path = "/api/providers/{name}/models", tag = "models",
    responses((status = 200, body = Vec<String>), (status = 404, body = ErrorOut), (status = 502, body = ErrorOut)))]
async fn list_provider_models(
    State(s): State<AppState>,
    Path(name): Path<String>,
) -> ApiResult<axum::Json<Vec<String>>> {
    Ok(axum::Json(s.services.models.list_models(&name).await?))
}

#[derive(Serialize, ToSchema)]
pub struct FeaturesOut {
    /// 配置了 [transcription]：界面显示语音输入
    pub transcription: bool,
}

/// 服务端配置决定的界面行为：可选能力。
#[utoipa::path(get, path = "/api/features", tag = "models", responses((status = 200, body = FeaturesOut)))]
async fn features(State(s): State<AppState>) -> axum::Json<FeaturesOut> {
    axum::Json(FeaturesOut { transcription: s.services.transcriber.is_some() })
}
