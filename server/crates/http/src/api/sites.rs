//! agent 自定义页面（#355）：界面列出各 agent 的页面，iframe 打开。
//!
//! 页面是 agent 写的 HTML + 脚本，要能执行，又不能与界面同源（同源就带着登录 cookie，能以用户身份调全部接口）。所以页面
//! 响应带 `Content-Security-Policy: sandbox allow-scripts …`（不含 allow-same-origin）：脚本照常跑，但页面是一个不透明的源，
//! 读不到 cookie、调不了 /api。沙箱里的请求不带 cookie，页面及其同目录文件改凭签名网址访问：
//! `/sites/<agent>/<页面>/<签名>/<文件>`，签名只有已登录的界面从 `GET /api/pages` 拿得到（[`crate::auth::Auth::sign_site`]）。
//! 页面里用相对路径读同目录的数据（`fetch('data.json')`）：那是从不透明的源发出的跨源请求，所以响应带
//! `Access-Control-Allow-Origin: *`；`Referrer-Policy: no-referrer` 免得页面加载外部资源时把签名网址带出去。内容随时会变，
//! 不缓存。

use axum::Router;
use axum::extract::State;
use axum::http::{HeaderName, HeaderValue};
use axum::response::{IntoResponse, Redirect, Response};
use axum::routing::get;
use chrono::{DateTime, Utc};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use serde::Serialize;
use utoipa::ToSchema;
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::api::assets::blocking;
use crate::auth::Auth;
use crate::download::file_response;
use crate::error::{ApiError, ApiResult};
use crate::extract::Path;
use crate::state::AppState;

pub const SITES_PATH: &str = "/sites";

/// 沙箱权限与前端 iframe 的 sandbox 属性（web/src/pages/SitePages.tsx SANDBOX）一致：浏览器取两者交集，改就两处一起改。
/// 签名不过期、按页面签：地址可以收藏、在新窗口常开；泄露了改密码即失效。
const PAGE_HEADERS: [(&str, &str); 5] = [
    ("content-security-policy", "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"),
    ("x-content-type-options", "nosniff"),
    ("access-control-allow-origin", "*"),
    ("referrer-policy", "no-referrer"),
    ("cache-control", "no-cache"),
];

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new().routes(routes!(list_pages))
}

/// 不用登录、凭签名访问的页面文件（不进接口描述）。
pub fn public_routes() -> Router<AppState> {
    Router::new()
        .route("/sites/{agent_id}/{page}/{sig}", get(page_root))
        .route("/sites/{agent_id}/{page}/{sig}/", get(page_index))
        .route("/sites/{agent_id}/{page}/{sig}/{*path}", get(page_file))
}

#[derive(Serialize, ToSchema)]
pub struct SitePageOut {
    pub agent_id: String,
    /// 页面目录名（site/<name>/）
    pub name: String,
    /// index.html 的 <title>，没有就是 name
    pub title: String,
    /// 页面目录里最近改动的文件的时间（数据文件刷新也算）
    pub modified: DateTime<Utc>,
    /// 打开页面的签名地址（以 / 结尾，页面里的相对路径据此解析）
    pub url: String,
}

/// 页面名在网址里编码（同 Python 的 `quote(page, safe='')`）
const PAGE_NAME: &AsciiSet = &NON_ALPHANUMERIC.remove(b'-').remove(b'.').remove(b'_').remove(b'~');

fn site_url(auth: &Auth, agent_id: &str, page: &str) -> String {
    let encoded = utf8_percent_encode(page, PAGE_NAME);
    format!("{SITES_PATH}/{agent_id}/{encoded}/{}/", auth.sign_site(agent_id, page))
}

/// 全部 agent 的页面：按 agent 列表的顺序，各自按页面名排序。
#[utoipa::path(get, path = "/api/pages", tag = "pages", responses((status = 200, body = Vec<SitePageOut>)))]
async fn list_pages(State(s): State<AppState>) -> ApiResult<axum::Json<Vec<SitePageOut>>> {
    let mut out = Vec::new();
    for agent in s.services.agents.list().await? {
        let sites = s.services.sites.clone();
        let id = agent.id.clone();
        let pages = tokio::task::spawn_blocking(move || sites.pages(&id)).await.expect("listing pages does not panic");
        out.extend(pages.into_iter().map(|p| SitePageOut {
            url: site_url(&s.auth, &agent.id, &p.name),
            agent_id: agent.id.clone(),
            name: p.name,
            title: p.title,
            modified: p.modified,
        }));
    }
    Ok(axum::Json(out))
}

fn check(s: &AppState, agent_id: &str, page: &str, sig: &str) -> ApiResult<()> {
    if s.auth.verify_site(agent_id, page, sig) { Ok(()) } else { Err(ApiError::not_found("Not Found")) }
}

/// 不带结尾斜杠时页面里的相对路径会解析到上一级，补上。
async fn page_root(
    State(s): State<AppState>,
    Path((agent_id, page, sig)): Path<(String, String, String)>,
) -> ApiResult<Response> {
    check(&s, &agent_id, &page, &sig)?;
    Ok(Redirect::temporary(&site_url(&s.auth, &agent_id, &page)).into_response())
}

async fn page_index(
    State(s): State<AppState>,
    Path((agent_id, page, sig)): Path<(String, String, String)>,
) -> ApiResult<Response> {
    serve(s, agent_id, page, sig, String::new()).await
}

async fn page_file(
    State(s): State<AppState>,
    Path((agent_id, page, sig, path)): Path<(String, String, String, String)>,
) -> ApiResult<Response> {
    serve(s, agent_id, page, sig, path).await
}

async fn serve(s: AppState, agent_id: String, page: String, sig: String, path: String) -> ApiResult<Response> {
    check(&s, &agent_id, &page, &sig)?;
    s.services.agents.get_active(&agent_id).await?;
    let sites = s.services.sites.clone();
    let file = blocking(move || sites.file(&agent_id, &page, &path)).await?;
    let headers = PAGE_HEADERS.iter().map(|(k, v)| (HeaderName::from_static(k), HeaderValue::from_static(v)));
    file_response(&file.path, &file.media_type, None, headers).await
}
