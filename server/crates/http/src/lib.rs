//! HTTP 适配：接口、SSE、登录、OpenAPI、前端静态文件。只用各领域的公开接口，不另写业务规则。
//!
//! - [`router`]：整个服务的路由（登录、要登录的接口、agent 页面、前端）；[`openapi`]：接口描述，web/openapi.json 由它生成。
//! - 接口按领域分在 [`api`] 下，每个模块给出自己的路由（[`utoipa_axum`]：路由与接口描述是同一份声明）。

pub mod api;
pub mod auth;
pub mod download;
pub mod error;
pub mod events;
pub mod extract;
pub mod openapi;
pub mod state;
pub mod web;

use axum::Router;
use axum::middleware;
use tower_http::compression::CompressionLayer;
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

pub use state::{AppState, HttpConfig, Services};

/// 不用登录的接口：登录、登出。
fn public_api() -> OpenApiRouter<AppState> {
    OpenApiRouter::new().routes(routes!(auth::login)).routes(routes!(auth::logout))
}

/// 要登录的接口（前面加 [`auth::require_auth`]）。
fn private_api() -> OpenApiRouter<AppState> {
    OpenApiRouter::new()
        .routes(routes!(auth::me))
        .routes(routes!(events::events))
        .merge(api::agents::routes())
        .merge(api::assets::routes())
        .merge(api::models::routes())
        .merge(api::sessions::routes())
        .merge(api::files::routes())
        .merge(api::workspace::routes())
        .merge(api::issues::routes())
        .merge(api::triggers::routes())
        .merge(api::notifications::routes())
        .merge(api::usage::routes())
        .merge(api::history::routes())
        .merge(api::speech::routes())
        .merge(api::sites::routes())
}

/// 接口描述（含 SSE 事件），与运行中的服务是同一份声明。
pub fn openapi() -> utoipa::openapi::OpenApi {
    let mut doc = public_api().split_for_parts().1;
    doc.merge(private_api().split_for_parts().1);
    openapi::finish(doc)
}

/// 整个服务的路由。
pub fn router(state: AppState) -> Router {
    let public = public_api().split_for_parts().0;
    let private = private_api()
        .split_for_parts()
        .0
        .route_layer(middleware::from_fn_with_state(state.clone(), auth::require_auth));
    public
        .merge(private)
        // agent 的页面在沙箱里跑、拿不到 cookie，凭签名网址访问；不进接口描述
        .merge(api::sites::public_routes())
        .fallback(web::spa)
        // 按浏览器的 Accept-Encoding 压缩：用户经代理从外网访问，JSON 和前端代码压缩后小一个数量级（#843）。
        // 默认规则不压事件流（text/event-stream，压了会被缓冲、事件延迟）、图片和很小的响应。文件下载也被压：
        // 已压缩的大文件白花 CPU、没有 Content-Length 就没有下载进度，很少见，接受
        .layer(CompressionLayer::new())
        .with_state(state)
}
