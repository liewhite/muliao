//! 接口的错误响应：一律是 `{"detail": "<原因>"}`。领域错误按种类映射成 4xx（[`ErrorKind::ScriptFailed`] 为 502），外部服务
//! 出错为 502 并带上原因；其余（库出错、存下的数据坏了、程序错误）记 error 日志，对外只说 500。
//!
//! 各领域的错误类型把领域错误包在自己的变体里（也会包着下层领域的错误）：[`Classify`] 是每种错误「是哪一类」的唯一判断，
//! 新的错误类型在这里加一个实现。

use axum::Json;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use muliao_platform::errors::{DomainError, ErrorKind};
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

/// 一个错误是哪一类。
pub enum Class<'a> {
    /// 调用方改了输入就能成（4xx）
    Domain(&'a DomainError),
    /// 写入口拒绝的资源文件（按输入不对处理）
    Rejected(String),
    /// 外部服务出错，原因给已登录的用户看（502）
    Upstream(String),
    /// 别的都是服务端的问题（500，原因只进日志）
    Internal,
}

pub trait Classify: std::error::Error {
    fn class(&self) -> Class<'_>;
}

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

    fn of(e: &(impl Classify + ?Sized)) -> Self {
        match e.class() {
            Class::Domain(d) => Self::from(d.clone()),
            Class::Rejected(reason) => Self::invalid(reason),
            Class::Upstream(detail) => Self::new(StatusCode::BAD_GATEWAY, detail),
            Class::Internal => {
                tracing::error!(error = %e, "request failed");
                Self::new(StatusCode::INTERNAL_SERVER_ERROR, "internal server error (see the service log)")
            }
        }
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

/// 每种领域错误类型都能直接 `?` 成 [`ApiError`]
macro_rules! from_classified {
    ($($t:ty),* $(,)?) => {$(
        impl From<$t> for ApiError {
            fn from(e: $t) -> Self {
                Self::of(&e)
            }
        }
    )*};
}

from_classified!(
    muliao_assets::Error,
    muliao_sessions::error::Error,
    muliao_issues::Error,
    muliao_triggers::error::Error,
    muliao_history::error::Error,
    muliao_workspace::error::Error,
    muliao_files::FilesError,
    muliao_speech::SpeechError,
    muliao_runtime::models::ListError,
    muliao_notifications::push::SubscribeError,
    rusqlite::Error,
);

impl Classify for muliao_assets::Error {
    fn class(&self) -> Class<'_> {
        use muliao_assets::Error as E;
        match self {
            E::Domain(d) => Class::Domain(d),
            E::Rejected(r) => Class::Rejected(r.to_string()),
            E::Db(_) | E::Io(_) => Class::Internal,
        }
    }
}

impl Classify for muliao_sessions::error::Error {
    fn class(&self) -> Class<'_> {
        use muliao_sessions::error::Error as E;
        match self {
            E::Domain(d) => Class::Domain(d),
            E::Assets(e) => e.class(),
            E::Files(e) => e.class(),
            E::Broken(_) | E::Db(_) | E::Subscriber(_) | E::Summarize(_) | E::Io(_) => Class::Internal,
        }
    }
}

impl Classify for muliao_issues::Error {
    fn class(&self) -> Class<'_> {
        use muliao_issues::Error as E;
        match self {
            E::Domain(d) => Class::Domain(d),
            E::Sessions(e) => e.class(),
            E::Assets(e) => e.class(),
            E::Broken(_) | E::Db(_) => Class::Internal,
        }
    }
}

impl Classify for muliao_triggers::error::Error {
    fn class(&self) -> Class<'_> {
        use muliao_triggers::error::Error as E;
        match self {
            E::Domain(d) => Class::Domain(d),
            E::Assets(e) => e.class(),
            E::Sessions(e) => e.class(),
            E::Corrupt(_) | E::Broken(_) | E::Db(_) | E::Io(_) => Class::Internal,
        }
    }
}

impl Classify for muliao_history::error::Error {
    fn class(&self) -> Class<'_> {
        use muliao_history::error::Error as E;
        match self {
            E::Domain(d) => Class::Domain(d),
            E::Sessions(e) => e.class(),
            E::Issues(e) => e.class(),
            E::Assets(e) => e.class(),
            E::Broken(_) | E::Db(_) => Class::Internal,
        }
    }
}

impl Classify for muliao_workspace::error::Error {
    fn class(&self) -> Class<'_> {
        use muliao_workspace::error::Error as E;
        match self {
            E::Domain(d) => Class::Domain(d),
            E::Assets(e) => e.class(),
            E::Sessions(e) => e.class(),
            E::Db(_) | E::Io(_) | E::Broken(_) => Class::Internal,
        }
    }
}

impl Classify for muliao_files::FilesError {
    fn class(&self) -> Class<'_> {
        use muliao_files::FilesError as E;
        match self {
            E::Domain(d) => Class::Domain(d),
            E::Corrupt(_) | E::Io(_) | E::Db(_) => Class::Internal,
        }
    }
}

impl Classify for muliao_speech::SpeechError {
    fn class(&self) -> Class<'_> {
        use muliao_speech::SpeechError as E;
        match self {
            E::Invalid(d) => Class::Domain(d),
            E::Upstream(detail) => Class::Upstream(detail.clone()),
        }
    }
}

impl Classify for muliao_runtime::models::ListError {
    fn class(&self) -> Class<'_> {
        use muliao_runtime::models::ListError as E;
        match self {
            E::Domain(d) => Class::Domain(d),
            E::Upstream { .. } => Class::Upstream(self.to_string()),
        }
    }
}

impl Classify for muliao_notifications::push::SubscribeError {
    fn class(&self) -> Class<'_> {
        use muliao_notifications::push::SubscribeError as E;
        match self {
            E::Invalid(e) => Class::Rejected(e.to_string()),
            E::Db(_) => Class::Internal,
        }
    }
}

impl Classify for rusqlite::Error {
    fn class(&self) -> Class<'_> {
        Class::Internal
    }
}
