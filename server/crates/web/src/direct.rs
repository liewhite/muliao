//! 直连抓取：抓取服务拿不到或不可用时的备用（#189、#309）。HTML 提取正文转成 Markdown，纯文本（源码、Markdown、JSON……）
//! 原样返回，都注明是直连。

use std::time::Duration;

use crate::{Page, WebError, host_of};

/// 整段（连接到读完）的时限
const TIMEOUT: Duration = Duration::from_secs(20);
/// 读到这么多就停：正文最终还会按工具输出上限截断，这里只防下载大文件
const MAX_BYTES: usize = 2 * 1024 * 1024;
const USER_AGENT: &str = "Mozilla/5.0 (compatible; muliao web_fetch)";
const HTML_TYPES: &[&str] = &["text/html", "application/xhtml+xml"];
/// 不经转换就能直接给模型读的非 text/* 类型
const TEXT_TYPES: &[&str] = &[
    "application/json",
    "application/xml",
    "application/javascript",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
];

struct Response {
    /// 跟随重定向后的最终地址：HTML 里的相对链接相对它
    url: String,
    content_type: String,
    /// content_type 里的媒体类型（小写、不带参数）
    media: String,
    body: Vec<u8>,
    /// 响应头声明且认识的编码；没声明或不认识时为 UTF-8
    encoding: &'static encoding_rs::Encoding,
}

fn is_readable(media: &str) -> bool {
    HTML_TYPES.contains(&media)
        || media.starts_with("text/")
        || TEXT_TYPES.contains(&media)
        || media.ends_with("+json")
        || media.ends_with("+xml")
}

/// 响应头里的 charset：认识的才用，站点写错的（如 charset=bogus）当作没声明
fn encoding_of(content_type: &str) -> &'static encoding_rs::Encoding {
    content_type
        .split(';')
        .skip(1)
        .filter_map(|p| p.trim().split_once('='))
        .find(|(k, _)| k.trim().eq_ignore_ascii_case("charset"))
        .and_then(|(_, v)| encoding_rs::Encoding::for_label(v.trim().trim_matches('"').as_bytes()))
        .unwrap_or(encoding_rs::UTF_8)
}

/// GET 一次；失败时的消息是给模型看的原因。
async fn get(client: &reqwest::Client, url: &str) -> Result<Response, String> {
    let read = async {
        let mut resp = client
            .get(url)
            .header(reqwest::header::USER_AGENT, USER_AGENT)
            .send()
            .await
            .map_err(|e| format!("a direct GET failed: {e}"))?;
        if resp.status().as_u16() != 200 {
            return Err(format!("a direct GET returned HTTP {}", resp.status().as_u16()));
        }
        let content_type = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_string();
        let media = content_type.split(';').next().unwrap_or_default().trim().to_lowercase();
        if !is_readable(&media) {
            let shown = if content_type.is_empty() { "(none)" } else { &content_type };
            return Err(format!(
                "a direct GET returned content-type {shown}, which web_fetch cannot read: try `run_shell` with curl"
            ));
        }
        let final_url = resp.url().to_string();
        let mut body = Vec::new();
        while let Some(chunk) = resp.chunk().await.map_err(|e| format!("a direct GET failed: {e}"))? {
            body.extend_from_slice(&chunk);
            if body.len() >= MAX_BYTES {
                body.truncate(MAX_BYTES);
                break;
            }
        }
        let encoding = encoding_of(&content_type);
        Ok(Response { url: final_url, content_type, media, body, encoding })
    };
    tokio::time::timeout(TIMEOUT, read)
        .await
        .unwrap_or_else(|_| Err(format!("a direct GET did not finish within {}s", TIMEOUT.as_secs())))
}

/// 用 readability 提取正文转成 Markdown（去掉导航、页脚等），相对链接按最终地址补全；提取不出正文时返回 None。
fn html_to_page(html: &str, url: &str) -> Option<Page> {
    let cfg = dom_smoothie::Config { text_mode: dom_smoothie::TextMode::Markdown, ..Default::default() };
    let article = dom_smoothie::Readability::new(html, Some(url), Some(cfg)).ok()?.parse().ok()?;
    let text = article.text_content.trim();
    if text.is_empty() {
        return None;
    }
    Some(Page { title: article.title, content: text.to_string() })
}

async fn readable_page(resp: &Response) -> Result<Page, String> {
    let (text, _, _) = resp.encoding.decode(&resp.body);
    if !HTML_TYPES.contains(&resp.media.as_str()) {
        return Ok(Page { title: String::new(), content: text.into_owned() });
    }
    let (html, url) = (text.into_owned(), resp.url.clone());
    // CPU 活，不占异步线程
    tokio::task::spawn_blocking(move || html_to_page(&html, &url))
        .await
        .unwrap_or_else(|e| std::panic::resume_unwind(e.into_panic()))
        .ok_or_else(|| {
            "a direct GET returned HTML without extractable text (the page is probably rendered by JavaScript): \
             open it in a browser instead"
                .to_string()
        })
}

/// 抓取服务拿不到或不可用（`service_reason`）时直接 GET 一次。直连 404 才说明页面确实不存在，所以服务那句 404 不能单独
/// 交还（#189）；拿不到时把两边的原因一起交还模型（[`WebError::Rejected`]），不算暂时性失败，包括服务本身也暂时不可用
/// 时：连不上的多半是目标站点（同 #134 的目标站点超时）。本机整体掉线时两边都报连不上，模型从两条原因里看得出来，由它
/// 决定稍后重试、换 URL 还是改用浏览器。
pub async fn fetch(client: &reqwest::Client, url: &str, service_reason: &str) -> Result<Page, WebError> {
    let host = host_of(url);
    let fetched = match get(client, url).await {
        Ok(resp) => readable_page(&resp).await.map(|page| (resp, page)),
        Err(e) => Err(e),
    };
    let (resp, page) = fetched.map_err(|e| {
        tracing::warn!(provider = "direct", op = "fetch", %host, reason = %e, "web request failed");
        WebError::Rejected(format!(
            "{service_reason}. The fetch service did not return this page, which does not by itself mean the page is \
             missing; {e}"
        ))
    })?;
    tracing::info!(provider = "direct", %host, chars = page.content.len(), "web fetch");
    let note = format!(
        "(Fetched directly from {} with a plain GET because the fetch service did not return it; content-type {}.)",
        resp.url, resp.content_type
    );
    Ok(Page { title: page.title, content: format!("{note}\n\n{}", page.content) })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn charset_from_header() {
        assert_eq!(encoding_of("text/html; charset=GBK"), encoding_rs::GBK);
        assert_eq!(encoding_of("text/html; Charset=\"utf-8\""), encoding_rs::UTF_8);
        assert_eq!(encoding_of("text/html; charset=bogus"), encoding_rs::UTF_8);
        assert_eq!(encoding_of("text/plain"), encoding_rs::UTF_8);
    }

    #[test]
    fn readable_types() {
        for t in ["text/html", "text/plain", "application/json", "application/ld+json", "image/svg+xml"] {
            assert!(is_readable(t), "{t}");
        }
        for t in ["application/pdf", "image/png", "application/octet-stream", ""] {
            assert!(!is_readable(t), "{t}");
        }
    }
}
