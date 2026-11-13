//! 接口的错误响应：一律是 `{"detail": "<原因>"}`。领域错误按种类映射成 4xx（[`ErrorKind::ScriptFailed`] 为 502），外部服务
//! 出错为 502 并带上原因；其余（库出错、存下的数据坏了、程序错误）记 error 日志，对外只说 500。
//! 各领域共用一个错误类型（[`Error`]），这里按它的三类映射。

use axum::Json;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use muliao_platform::errors::{DomainError, Error, ErrorKind};
use serde::Serialize;
use utoipa::ToSchema;

/// 错误响应体（各接口的 4xx、5xx 都是它）。
#[derive(Debug, Serialize, ToSchema)]
pub struct ErrorOut {
    pub detail: String,
}

#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    detail: String,
    retry_after: Option<u64>,
}

pub type ApiResult<T> = Result<T, ApiError>;

impl ApiError {
    pub fn new(status: StatusCode, detail: impl Into<String>) -> Self {
        Self { status, detail: detail.into(), retry_after: None }
    }

    pub fn not_found(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, detail)
    }

    pub fn invalid(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, detail)
    }

    /// 登录退避期内：429 带 Retry-After
    pub fn throttled(seconds: u64) -> Self {
        Self {
            status: StatusCode::TOO_MANY_REQUESTS,
            detail: format!("too many failed logins, retry in {seconds}s"),
            retry_after: Some(seconds),
        }
    }

    pub fn status(&self) -> StatusCode {
        self.status
    }
}

impl From<DomainError> for ApiError {
    fn from(d: DomainError) -> Self {
        let status = match d.kind {
            ErrorKind::NotFound => StatusCode::NOT_FOUND,
            ErrorKind::Conflict => StatusCode::CONFLICT,
            ErrorKind::InvalidInput => StatusCode::UNPROCESSABLE_ENTITY,
            ErrorKind::Unauthorized => StatusCode::UNAUTHORIZED,
            ErrorKind::Forbidden => StatusCode::FORBIDDEN,
            ErrorKind::TooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            // 如手动运行触发器时前置检查失败：原因（可能含脚本 stderr）给已登录的用户看
            ErrorKind::ScriptFailed => StatusCode::BAD_GATEWAY,
        };
        Self::new(status, d.message)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut response = (self.status, Json(ErrorOut { detail: self.detail })).into_response();
        if let Some(seconds) = self.retry_after {
            response.headers_mut().insert(header::RETRY_AFTER, HeaderValue::from(seconds));
        }
        response
    }
}

impl From<Error> for ApiError {
    fn from(e: Error) -> Self {
        match e {
            Error::Domain(d) => Self::from(d),
            // 原因给已登录的用户看
            Error::Upstream { message, .. } => Self::new(StatusCode::BAD_GATEWAY, message),
            Error::Internal(e) => {
                tracing::error!(error = %e, "request failed");
                Self::new(StatusCode::INTERNAL_SERVER_ERROR, "internal server error (see the service log)")
            }
        }
    }
}

impl From<rusqlite::Error> for ApiError {
    fn from(e: rusqlite::Error) -> Self {
        Error::from(e).into()
    }
}
