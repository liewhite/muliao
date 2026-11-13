//! 触发器的管理：列出、新建、修改、删除、立即运行。
//!
//! 触发器的配置与状态在领域里是各触发方式自己解释的 JSON（加一种方式只加代码），这里原样交进交出；接口描述里按 kind 写出
//! 各方式的具体形状（[`KindOutDoc`]、[`KindInDoc`]），加一种方式时在这两处各加一个变体（有测试对照登记的方式）。

use axum::extract::State;
use axum::http::StatusCode;
use chrono::{DateTime, Utc};
use muliao_triggers::faults::TriggerFault;
use muliao_triggers::once::{OnceConfig, OnceStatus};
use muliao_triggers::precheck::Precheck;
use muliao_triggers::schedule::{ScheduleConfig, ScheduleStatus};
use muliao_triggers::store::Listing;
use muliao_triggers::stream::{StreamConfig, StreamStatus};
use muliao_triggers::{NewSpec, PrecheckChange, TriggerView, Update};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use utoipa::{IntoParams, ToSchema};
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::error::{ApiResult, ErrorOut};
use crate::extract::{Json, Path, Query};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new()
        .routes(routes!(trigger_kinds))
        .routes(routes!(list_triggers, create_trigger))
        .routes(routes!(update_trigger, delete_trigger))
        .routes(routes!(run_trigger))
}

#[derive(Serialize, ToSchema)]
pub struct TriggerKindOut {
    pub name: String,
    /// 什么时候触发、config 的格式
    pub description: String,
}

/// 登记的触发方式。
#[utoipa::path(get, path = "/api/trigger-kinds", tag = "triggers", responses((status = 200, body = Vec<TriggerKindOut>)))]
async fn trigger_kinds(State(s): State<AppState>) -> axum::Json<Vec<TriggerKindOut>> {
    let kinds = s.services.triggers.kinds().all().iter();
    axum::Json(
        kinds.map(|k| TriggerKindOut { name: k.name().to_owned(), description: k.description().to_owned() }).collect(),
    )
}

/// 一个触发器：各方式共有的字段，加上按 kind 区分的 config 与 status。
#[derive(Serialize, ToSchema)]
pub struct TriggerOut {
    id: i64,
    name: String,
    agent_id: String,
    prompt: String,
    enabled: bool,
    /// 触发前的前置检查；没有则每次都交给 agent
    precheck: Option<Precheck>,
    last_checked_at: Option<DateTime<Utc>>,
    last_fired_at: Option<DateTime<Utc>>,
    /// 上一次触发开出的、还在的会话
    last_session_id: Option<String>,
    /// 被自动停用的原因（重新启用时清除）
    disabled_reason: Option<String>,
    /// 脚本出错：前置检查失败、流式命令反复退出
    faults: Vec<TriggerFault>,
    /// 结束的时间：不为空表示它不会再触发了（一次性触发器跑完），只能查看与删除
    finished_at: Option<DateTime<Utc>>,
    /// 存下的运行状态坏了、读不出时的原因（这时 status 为 null）
    status_error: Option<String>,
    #[serde(flatten)]
    #[schema(value_type = KindOutDoc)]
    kind: KindOut,
}

/// 领域里的 kind、config 与 status（各方式自己解释的 JSON）；接口描述里的形状是 [`KindOutDoc`]。
#[derive(Serialize)]
struct KindOut {
    kind: String,
    config: Value,
    status: Option<Value>,
}

/// config 与 status（这种方式自己给出的运行状态，不含凭证；读不出时为 null，原因在 status_error）按 kind 区分。
#[derive(ToSchema)]
#[allow(dead_code)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum KindOutDoc {
    Schedule { config: ScheduleConfig, status: Option<ScheduleStatus> },
    Once { config: OnceConfig, status: Option<OnceStatus> },
    Stream { config: StreamConfig, status: Option<StreamStatus> },
}

impl TriggerOut {
    fn of(v: TriggerView) -> Self {
        let t = v.trigger;
        let (status, status_error) = match v.status {
            Ok(status) => (Some(status), None),
            Err(e) => (None, Some(e)),
        };
        TriggerOut {
            id: t.id,
            name: t.name,
            agent_id: t.agent_id,
            prompt: t.prompt,
            enabled: t.enabled,
            precheck: t.precheck,
            last_checked_at: t.last_checked_at,
            last_fired_at: t.last_fired_at,
            last_session_id: v.live_session_id,
            disabled_reason: t.disabled_reason,
            faults: t.faults,
            finished_at: t.finished_at,
            status_error,
            kind: KindOut { kind: t.kind, config: t.config, status },
        }
    }
}

#[derive(Deserialize, IntoParams)]
#[into_params(parameter_in = Query)]
pub struct TriggerQuery {
    /// 列已结束的（跑完的一次性触发器）而不是还会触发的
    #[serde(default)]
    pub finished: bool,
}

#[utoipa::path(get, path = "/api/triggers", tag = "triggers", params(TriggerQuery),
    responses((status = 200, body = Vec<TriggerOut>)))]
async fn list_triggers(
    State(s): State<AppState>,
    Query(q): Query<TriggerQuery>,
) -> ApiResult<axum::Json<Vec<TriggerOut>>> {
    let t = &s.services.triggers;
    let found = t.list(Listing { kind: None, enabled_only: false, finished: q.finished }).await?;
    Ok(axum::Json(t.views(found).await?.into_iter().map(TriggerOut::of).collect()))
}

/// 新建触发器：kind 与 config 按方式区分（有默认值的配置项可以不填），config 原样交给触发方式校验、补默认值。
#[derive(Deserialize, ToSchema)]
pub struct TriggerCreate {
    name: String,
    agent_id: String,
    prompt: String,
    /// 触发前的前置检查；不给则每次都交给 agent
    #[serde(default)]
    precheck: Option<Precheck>,
    #[serde(flatten)]
    #[schema(value_type = KindInDoc)]
    kind: KindIn,
}

#[derive(Deserialize)]
struct KindIn {
    kind: String,
    config: Value,
}

#[derive(ToSchema)]
#[allow(dead_code)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum KindInDoc {
    Schedule { config: ScheduleConfig },
    Once { config: OnceConfig },
    Stream { config: StreamConfig },
}

#[utoipa::path(post, path = "/api/triggers", tag = "triggers", request_body = TriggerCreate,
    responses((status = 201, body = TriggerOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 422, body = ErrorOut)))]
async fn create_trigger(
    State(s): State<AppState>,
    Json(body): Json<TriggerCreate>,
) -> ApiResult<(StatusCode, axum::Json<TriggerOut>)> {
    let t = &s.services.triggers;
    let spec = NewSpec {
        name: body.name,
        agent_id: body.agent_id,
        kind: body.kind.kind,
        prompt: body.prompt,
        config: body.kind.config,
        precheck: body.precheck,
    };
    let created = t.create(spec).await?;
    Ok((StatusCode::CREATED, axum::Json(TriggerOut::of(t.view(created).await?))))
}

/// 只修改给出的字段。precheck 给出 null 表示去掉前置检查，不出现表示不变。
#[derive(Deserialize, ToSchema)]
pub struct TriggerPatch {
    #[serde(default)]
    pub prompt: Option<String>,
    /// 修改请求不带 kind：按已存的 kind 校验，格式同新建时这种方式的 config
    #[serde(default)]
    #[schema(value_type = Option<Object>)]
    pub config: Option<Value>,
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default, deserialize_with = "crate::extract::present")]
    #[schema(value_type = Option<Precheck>)]
    pub precheck: Option<Option<Precheck>>,
}

#[utoipa::path(patch, path = "/api/triggers/{trigger_id}", tag = "triggers", request_body = TriggerPatch,
    responses((status = 200, body = TriggerOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 422, body = ErrorOut)))]
async fn update_trigger(
    State(s): State<AppState>,
    Path(trigger_id): Path<i64>,
    Json(body): Json<TriggerPatch>,
) -> ApiResult<axum::Json<TriggerOut>> {
    let t = &s.services.triggers;
    let precheck = match body.precheck {
        None => PrecheckChange::Keep,
        Some(None) => PrecheckChange::Remove,
        Some(Some(p)) => PrecheckChange::Set(p),
    };
    let update = Update { prompt: body.prompt, config: body.config, enabled: body.enabled, precheck };
    let updated = t.update(trigger_id, update).await?;
    Ok(axum::Json(TriggerOut::of(t.view(updated).await?)))
}

#[utoipa::path(delete, path = "/api/triggers/{trigger_id}", tag = "triggers",
    responses((status = 204), (status = 404, body = ErrorOut)))]
async fn delete_trigger(State(s): State<AppState>, Path(trigger_id): Path<i64>) -> ApiResult<StatusCode> {
    s.services.triggers.delete(trigger_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Serialize, ToSchema)]
pub struct FiredOut {
    /// 是否投出了信（前置检查判定这次不触发时为 false）
    pub fired: bool,
    /// 这次新开的会话；没触发时为 null
    pub session_id: Option<String>,
}

/// 立即触发一次（不带事件内容）。有前置检查时同样先跑检查，可以用来测试整条链路（检查失败 502 带原因）。不消耗一次性
/// 触发器预定的那一次；已结束的触发器不能再运行。
#[utoipa::path(post, path = "/api/triggers/{trigger_id}/run", tag = "triggers",
    responses((status = 202, body = FiredOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 502, body = ErrorOut)))]
async fn run_trigger(
    State(s): State<AppState>,
    Path(trigger_id): Path<i64>,
) -> ApiResult<(StatusCode, axum::Json<FiredOut>)> {
    let fired = s.services.triggers.run(trigger_id).await?;
    let out = FiredOut { fired: fired.is_some(), session_id: fired.map(|f| f.session_id) };
    Ok((StatusCode::ACCEPTED, axum::Json(out)))
}
