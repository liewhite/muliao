//! 对话里的文件：上传（先上传拿到 id，发消息时引用）与下载。做法同 Matrix 的媒体接口。
//!
//! 下载与界面同源、带着登录 cookie，所以内容一律按不可信处理（nosniff 加 CSP sandbox），只有能内联的图片按 inline 返回，
//! 其余（HTML、SVG……）一律作为附件下载，不在本站执行。

use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::Response;
use chrono::{DateTime, Utc};
use futures::TryStreamExt;
use muliao_files::FileMeta;
use muliao_platform::ids::{FileId, MediaType};
use serde::{Deserialize, Serialize};
use tokio_util::io::StreamReader;
use utoipa::{IntoParams, ToSchema};
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::download::{Disposition, file_response, untrusted_headers};
use crate::error::{ApiError, ApiResult, ErrorOut};
use crate::extract::{Path, Query};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new().routes(routes!(upload)).routes(routes!(download))
}

#[derive(Clone, Serialize, ToSchema)]
pub struct FileOut {
    pub id: FileId,
    pub name: String,
    pub media_type: MediaType,
    pub size: u64,
    pub url: String,
    /// 能内联显示的图片；其余是下载链接
    pub inline: bool,
    pub created_at: DateTime<Utc>,
}

impl FileOut {
    pub fn of(f: &FileMeta) -> Self {
        FileOut {
            id: f.id.clone(),
            name: f.name.clone(),
            media_type: f.media_type.clone(),
            size: f.size,
            url: f.url(),
            inline: f.media_type.is_inline_image(),
            created_at: f.created_at,
        }
    }
}

#[derive(Deserialize, IntoParams)]
#[into_params(parameter_in = Query)]
pub struct UploadQuery {
    /// 文件名
    pub name: String,
}

/// 请求的 Content-Type（上传的文件、语音的类型）；没有或不是 type/subtype 是输入错误
pub fn content_type(headers: &HeaderMap, what: &str) -> Result<MediaType, ApiError> {
    let raw = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
        .ok_or_else(|| ApiError::invalid(format!("Content-Type header is required: it is the type of {what}")))?;
    Ok(MediaType::parse(raw)?)
}

/// 请求体就是文件内容，Content-Type 是它的类型（与 Matrix /media/upload 相同，不用 multipart），边读边写，超限即停。
#[utoipa::path(post, path = "/api/files", tag = "files", params(UploadQuery),
    request_body(content = String, content_type = "application/octet-stream", description = "文件内容（任意类型）"),
    responses((status = 201, body = FileOut), (status = 413, body = ErrorOut), (status = 422, body = ErrorOut)))]
async fn upload(
    State(s): State<AppState>,
    Query(q): Query<UploadQuery>,
    headers: HeaderMap,
    body: Body,
) -> ApiResult<(StatusCode, axum::Json<FileOut>)> {
    let media_type = content_type(&headers, "the uploaded file")?;
    let reader = StreamReader::new(body.into_data_stream().map_err(std::io::Error::other));
    let meta = s.services.files.put(&q.name, media_type, reader).await?;
    Ok((StatusCode::CREATED, axum::Json(FileOut::of(&meta))))
}

/// 文件内容；每个文件只有一个规范地址（名字对不上 404）。内容按 id 永不改变，可以一直缓存。
#[utoipa::path(get, path = "/api/files/{file_id}/{name}", tag = "files",
    responses((status = 200, description = "文件内容"), (status = 404, body = ErrorOut)))]
async fn download(State(s): State<AppState>, Path((file_id, name)): Path<(FileId, String)>) -> ApiResult<Response> {
    let files = &s.services.files;
    let meta = files.get(&file_id).await?;
    if name != meta.name {
        return Err(ApiError::not_found(format!("file {file_id:?} is not named {name:?}")));
    }
    let disposition = if meta.media_type.is_inline_image() { Disposition::Inline } else { Disposition::Attachment };
    let immutable = (header::CACHE_CONTROL, HeaderValue::from_static("private, max-age=31536000, immutable"));
    file_response(
        &files.blob_path(&meta),
        meta.media_type.as_str(),
        Some((disposition, &meta.name)),
        untrusted_headers().chain([immutable]),
    )
    .await
}
