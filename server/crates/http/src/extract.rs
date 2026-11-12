//! 请求的解析：JSON 请求体、查询参数、路径参数。解析不了时同其他错误一样回 `{"detail": …}`（422），前端只认这一种错误体。
//! 查询参数支持同名参数重复（`?status=a&status=b` 读成列表）。

use axum::extract::rejection::{JsonRejection, PathRejection};
use axum::extract::{FromRequest, FromRequestParts, Request};
use axum::http::request::Parts;
use serde::Deserialize;
use serde::de::DeserializeOwned;

use crate::error::ApiError;

pub struct Json<T>(pub T);

impl<S: Send + Sync, T: DeserializeOwned> FromRequest<S> for Json<T> {
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, ApiError> {
        match axum::Json::<T>::from_request(req, state).await {
            Ok(axum::Json(value)) => Ok(Json(value)),
            Err(JsonRejection::BytesRejection(e)) => Err(ApiError::new(e.status(), e.body_text())),
            Err(e) => Err(ApiError::invalid(e.body_text())),
        }
    }
}

pub struct Query<T>(pub T);

impl<S: Send + Sync, T: DeserializeOwned> FromRequestParts<S> for Query<T> {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, ApiError> {
        let query = parts.uri.query().unwrap_or_default();
        serde_html_form::from_str(query)
            .map(Query)
            .map_err(|e| ApiError::invalid(format!("invalid query parameters: {e}")))
    }
}

pub struct Path<T>(pub T);

impl<S: Send + Sync, T: DeserializeOwned + Send> FromRequestParts<S> for Path<T> {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, ApiError> {
        match axum::extract::Path::<T>::from_request_parts(parts, state).await {
            Ok(axum::extract::Path(value)) => Ok(Path(value)),
            Err(e @ PathRejection::FailedToDeserializePathParams(_)) => Err(ApiError::invalid(e.body_text())),
            Err(e) => Err(ApiError::new(e.status(), e.body_text())),
        }
    }
}

/// 请求体里「没给」（None）与「给了 null」（Some(None)）要分开的字段：`#[serde(default, deserialize_with = "present")]`
pub fn present<'de, D: serde::Deserializer<'de>, T: serde::Deserialize<'de>>(
    d: D,
) -> Result<Option<Option<T>>, D::Error> {
    Option::<T>::deserialize(d).map(Some)
}
