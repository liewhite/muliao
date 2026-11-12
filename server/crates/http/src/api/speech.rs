//! 语音输入：界面录音后把原始音频发到这里，拿回文字再作为消息发出（同 /api/files：请求体就是内容，Content-Type 是类型）。

use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode, header};
use futures::StreamExt;
use muliao_platform::errors::{DomainError, ErrorKind};
use serde::Serialize;
use utoipa::ToSchema;
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::api::files::content_type;
use crate::error::{ApiError, ApiResult, ErrorOut};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new().routes(routes!(transcribe))
}

#[derive(Serialize, ToSchema)]
pub struct TranscriptOut {
    /// 听不到人说话时为空字符串
    pub text: String,
}

/// 按字节上限读请求体：声明的长度超限直接拒绝，未声明长度的边读边数，超限即停。
async fn read_limited(headers: &HeaderMap, body: Body, limit: u64) -> ApiResult<Vec<u8>> {
    let too_large =
        || ApiError::from(DomainError::new(ErrorKind::TooLarge, format!("request body exceeds {limit} bytes")));
    let declared =
        headers.get(header::CONTENT_LENGTH).and_then(|v| v.to_str().ok()).and_then(|v| v.parse::<u64>().ok());
    if declared.is_some_and(|n| n > limit) {
        return Err(too_large());
    }
    let mut stream = body.into_data_stream();
    let mut out = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk
            .map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, format!("reading the request body failed: {e}")))?;
        out.extend_from_slice(&chunk);
        if out.len() as u64 > limit {
            return Err(too_large());
        }
    }
    Ok(out)
}

/// 请求体是一段语音（webm、ogg、mp4、wav、mp3……），返回转写出的文字；转写服务出错时 502 并带上原因；没配置语音转写时 404。
#[utoipa::path(post, path = "/api/transcriptions", tag = "speech",
    request_body(content = String, content_type = "application/octet-stream", description = "一段语音"),
    responses((status = 200, body = TranscriptOut), (status = 404, body = ErrorOut), (status = 413, body = ErrorOut),
        (status = 422, body = ErrorOut), (status = 502, body = ErrorOut)))]
async fn transcribe(State(s): State<AppState>, headers: HeaderMap, body: Body) -> ApiResult<axum::Json<TranscriptOut>> {
    let Some(transcriber) = s.services.transcriber.clone() else {
        return Err(ApiError::not_found("speech transcription is not configured ([transcription] in config.toml)"));
    };
    let media_type = content_type(&headers, "the audio")?.to_owned();
    let audio = read_limited(&headers, body, transcriber.max_bytes()).await?;
    Ok(axum::Json(TranscriptOut { text: transcriber.transcribe(&audio, &media_type).await? }))
}
