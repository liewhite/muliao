//! 通知（收件箱、未读、标记已读）与推送订阅（VAPID 公钥、订阅、测试推送）。

use axum::extract::State;
use axum::http::StatusCode;
use muliao_notifications::{Notification, Subscription};
use serde::{Deserialize, Serialize};
use utoipa::{IntoParams, ToSchema};
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::error::{ApiResult, ErrorOut};
use crate::extract::{Json, Path, Query};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new()
        .routes(routes!(list_notifications))
        .routes(routes!(unread))
        .routes(routes!(mark_notifications_read))
        .routes(routes!(mark_issue_read))
        .routes(routes!(vapid_key))
        .routes(routes!(subscribe))
        .routes(routes!(test_push))
}

#[derive(Deserialize, IntoParams)]
#[into_params(parameter_in = Query)]
pub struct ListQuery {
    /// 条数，不给取默认；超出范围 422
    pub limit: Option<usize>,
}

/// 通知，新的在前。
#[utoipa::path(get, path = "/api/notifications", tag = "notifications", params(ListQuery),
    responses((status = 200, body = Vec<Notification>), (status = 422, body = ErrorOut)))]
async fn list_notifications(
    State(s): State<AppState>,
    Query(q): Query<ListQuery>,
) -> ApiResult<axum::Json<Vec<Notification>>> {
    let limit = q.limit.unwrap_or(muliao_notifications::inbox::LIST_DEFAULT);
    Ok(axum::Json(s.services.notifications.list(limit).await?))
}

#[derive(Serialize, ToSchema)]
pub struct UnreadOut {
    pub count: i64,
}

#[utoipa::path(get, path = "/api/notifications/unread", tag = "notifications", responses((status = 200, body = UnreadOut)))]
async fn unread(State(s): State<AppState>) -> ApiResult<axum::Json<UnreadOut>> {
    Ok(axum::Json(UnreadOut { count: s.services.notifications.unread_count().await? }))
}

#[derive(Deserialize, ToSchema)]
pub struct MarkReadIn {
    /// 用户看到的最新一条；之后才到的保持未读
    pub through_id: i64,
}

#[utoipa::path(post, path = "/api/notifications/read", tag = "notifications", request_body = MarkReadIn,
    responses((status = 204)))]
async fn mark_notifications_read(State(s): State<AppState>, Json(body): Json<MarkReadIn>) -> ApiResult<StatusCode> {
    s.services.notifications.mark_read(body.through_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// 打开了 issue：请用户审核它的通知标为已读。
#[utoipa::path(post, path = "/api/notifications/issues/{issue_id}/read", tag = "notifications", responses((status = 204)))]
async fn mark_issue_read(State(s): State<AppState>, Path(issue_id): Path<i64>) -> ApiResult<StatusCode> {
    s.services.notifications.mark_issue_read(issue_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Serialize, ToSchema)]
pub struct VapidOut {
    pub public_key: String,
}

#[utoipa::path(get, path = "/api/push/vapid", tag = "push", responses((status = 200, body = VapidOut)))]
async fn vapid_key(State(s): State<AppState>) -> axum::Json<VapidOut> {
    axum::Json(VapidOut { public_key: s.services.notifications.push().public_key().to_owned() })
}

#[derive(Deserialize, ToSchema)]
pub struct PushKeys {
    pub p256dh: String,
    pub auth: String,
}

/// 浏览器给的推送订阅（PushSubscription.toJSON() 的 endpoint 与 keys）。
#[derive(Deserialize, ToSchema)]
pub struct PushSubscriptionIn {
    pub endpoint: String,
    pub keys: PushKeys,
}

/// 登记这台设备的推送订阅（同一个 endpoint 再登记是更新）；订阅不合格式（不是 https、密钥解不开）422。
#[utoipa::path(post, path = "/api/push/subscriptions", tag = "push", request_body = PushSubscriptionIn,
    responses((status = 204), (status = 422, body = ErrorOut)))]
async fn subscribe(State(s): State<AppState>, Json(body): Json<PushSubscriptionIn>) -> ApiResult<StatusCode> {
    let device = Subscription { endpoint: body.endpoint, p256dh: body.keys.p256dh, auth: body.keys.auth };
    s.services.notifications.push().subscribe(device).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// 发一条测试通知（推到每台设备）。
#[utoipa::path(post, path = "/api/push/test", tag = "push", responses((status = 202)))]
async fn test_push(State(s): State<AppState>) -> ApiResult<StatusCode> {
    s.services.notifications.send_test().await?;
    Ok(StatusCode::ACCEPTED)
}
