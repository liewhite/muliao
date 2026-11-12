//! 把一个文件作为响应发出去（边读边发）。对话里的文件、工作目录与 skill 目录的浏览、agent 页面、前端静态文件共用。

use std::path::Path;

use axum::body::Body;
use axum::http::{HeaderName, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use tokio_util::io::ReaderStream;

use crate::error::{ApiError, ApiResult};

/// 文件名里原样保留的字符（RFC 5987 的 attr-char），其余按 UTF-8 百分号编码
const ATTR_CHAR: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'!')
    .remove(b'#')
    .remove(b'$')
    .remove(b'&')
    .remove(b'+')
    .remove(b'-')
    .remove(b'.')
    .remove(b'^')
    .remove(b'_')
    .remove(b'`')
    .remove(b'|')
    .remove(b'~');

/// 内容由 agent 或用户提供、与界面同源（带着登录 cookie）的文件：一律按不可信内容返回，不在本站执行（同 files 领域的约定）。
pub fn untrusted_headers() -> impl Iterator<Item = (HeaderName, HeaderValue)> {
    muliao_files::UNTRUSTED_CONTENT_HEADERS
        .iter()
        .map(|(k, v)| (HeaderName::try_from(*k).expect("valid header name"), HeaderValue::from_static(v)))
}

#[derive(Clone, Copy)]
pub enum Disposition {
    /// 浏览器里直接显示（只给能内联的图片）
    Inline,
    /// 作为附件下载
    Attachment,
}

/// Content-Disposition：纯 ASCII 的名字直接写，其余用 `filename*=utf-8''…`（同 Starlette 的 FileResponse）。
fn content_disposition(disposition: Disposition, filename: &str) -> HeaderValue {
    let kind = match disposition {
        Disposition::Inline => "inline",
        Disposition::Attachment => "attachment",
    };
    let encoded = utf8_percent_encode(filename, ATTR_CHAR).to_string();
    let value = if encoded == filename && !filename.contains('"') {
        format!("{kind}; filename=\"{filename}\"")
    } else {
        format!("{kind}; filename*=utf-8''{encoded}")
    };
    HeaderValue::from_str(&value).expect("percent-encoded header value is ASCII")
}

/// 一个文件的响应：media_type 是它的类型；disposition 给出时带上文件名；extra 是额外的响应头。文件打不开时 404。
pub async fn file_response(
    path: &Path,
    media_type: &str,
    disposition: Option<(Disposition, &str)>,
    extra: impl IntoIterator<Item = (HeaderName, HeaderValue)>,
) -> ApiResult<Response> {
    let file =
        tokio::fs::File::open(path).await.map_err(|e| ApiError::not_found(format!("cannot open the file: {e}")))?;
    let len = file.metadata().await.map_err(|e| ApiError::not_found(format!("cannot read the file: {e}")))?.len();
    let mut response = (StatusCode::OK, Body::from_stream(ReaderStream::new(file))).into_response();
    let headers = response.headers_mut();
    headers.insert(header::CONTENT_LENGTH, HeaderValue::from(len));
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_str(media_type).unwrap_or(HeaderValue::from_static("application/octet-stream")),
    );
    if let Some((disposition, filename)) = disposition {
        headers.insert(header::CONTENT_DISPOSITION, content_disposition(disposition, filename));
    }
    headers.extend(extra);
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disposition_encodes_non_ascii_names() {
        assert_eq!(content_disposition(Disposition::Attachment, "a b.txt"), "attachment; filename*=utf-8''a%20b.txt");
        assert_eq!(content_disposition(Disposition::Inline, "x.png"), "inline; filename=\"x.png\"");
        assert_eq!(
            content_disposition(Disposition::Attachment, "报告.pdf"),
            "attachment; filename*=utf-8''%E6%8A%A5%E5%91%8A.pdf"
        );
    }
}
