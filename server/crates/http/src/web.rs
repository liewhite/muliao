//! 前端静态文件：构建目录里有的文件原样返回，其余路径都返回 index.html（单页应用自己按路径显示页面）。`/api/` 下没有的
//! 接口是 404，不返回页面。

use std::path::Path;

use axum::extract::State;
use axum::http::{HeaderValue, Uri, header};
use axum::response::Response;
use sha2::{Digest, Sha256};

use crate::download::file_response;
use crate::error::{ApiError, ApiResult};
use crate::state::AppState;

/// 前端构建的版本：index.html 引用的资源文件名带内容哈希，它的内容变了就是换了构建。没有构建前端时为 None。
pub fn ui_version(web_dir: &Path) -> Option<String> {
    let index = std::fs::read(web_dir.join("index.html")).ok()?;
    Some(Sha256::digest(&index).iter().take(6).map(|b| format!("{b:02x}")).collect())
}

pub async fn spa(State(state): State<AppState>, uri: Uri) -> ApiResult<Response> {
    let path = uri.path().trim_start_matches('/');
    if path == "api" || path.starts_with("api/") {
        return Err(ApiError::not_found("Not Found"));
    }
    if state.ui_version.is_none() {
        return Err(ApiError::not_found("the web UI is not built; only the API is served"));
    }
    let decoded = percent_encoding::percent_decode_str(path).decode_utf8_lossy();
    // 解不出、出了构建目录的，按单页应用的路径处理
    if !decoded.is_empty()
        && let Ok(file) = muliao_platform::fs::resolve_within(&state.web_dir, &decoded)
        && file.as_path().is_file()
    {
        let media_type = mime_guess::from_path(file.as_path()).first_or_octet_stream();
        return file_response(file.as_path(), media_type.essence_str(), None, []).await;
    }
    let no_cache = (header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    file_response(&state.web_dir.join("index.html"), "text/html; charset=utf-8", None, [no_cache]).await
}
