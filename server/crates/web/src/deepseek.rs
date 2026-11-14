//! DeepSeek 的搜索与抓取：https://api-docs.deepseek.com/zh-cn/guides/search

use std::time::Duration;

use serde::Deserialize;
use serde::de::DeserializeOwned;

use muliao_platform::errors::{Error, Result, invalid};

use crate::{Page, SearchHit, direct, host_of};

pub const BASE_URL: &str = "https://api.deepseek.com";
/// 抓取要等对方站点，给足时间；挂起时及时报错，而不是一直占着这次运行
const TIMEOUT: Duration = Duration::from_secs(60);
/// 文档：408、429、5xx 等待后重试（fetch 带契约错误体的 408/504 例外，是目标站点超时，见 FETCH_UNAVAILABLE）
const TRANSIENT: &[u16] = &[408, 429, 500, 502, 503, 504];
/// 带契约错误体时，输入无效（400 字段值无效、413 请求体过大）：交还模型修正
const REJECTED_INPUT: &[u16] = &[400, 413];
/// 带契约错误体时 fetch 的「目标抓不到」：403 被安全策略拒绝、404 抓取不到可用内容、408/504 目标站点抓取超时（#134）。
/// 抓取服务在国内，境外站点常被墙或超时，它拿不到不说明页面不存在（实测 raw.githubusercontent.com 的纯文本文件都是
/// 404，#189），改为直连核实（#309）。其余（401 key 无效、402 余额不足、search 的 403/404……）是配置、账户问题或契约
/// 之外的响应，模型改不了，让这一轮失败可见
const FETCH_UNAVAILABLE: &[u16] = &[403, 404, 408, 504];

#[derive(Deserialize)]
struct SearchResult {
    title: String,
    url: String,
    summary: String,
    site_name: Option<String>,
    date: Option<String>,
}

#[derive(Deserialize)]
struct SearchResponse {
    results: Vec<SearchResult>,
}

#[derive(Deserialize)]
struct FetchResponse {
    title: String,
    content: String,
}

/// 文档约定的错误体 `{"error": {"message": ...}}`
#[derive(Deserialize)]
struct ErrorBody {
    error: ErrorMessage,
}

#[derive(Deserialize)]
struct ErrorMessage {
    message: String,
}

/// 一次请求没有成功
struct Failure {
    /// 没有收到完整响应（连不上、超时、读响应体出错）时为 None
    status: Option<u16>,
    /// 带着契约错误体：只有这样的才算服务按契约拒绝；状态码对上而错误体不对是网关或配置问题（网关自己的 504 页面、
    /// base_url 指错），按暂时性或配置失败处理
    contract: bool,
    reason: String,
}

impl Failure {
    /// fetch 的目标抓不到（见 FETCH_UNAVAILABLE）
    fn target_unavailable(&self) -> bool {
        self.contract && self.status.is_some_and(|s| FETCH_UNAVAILABLE.contains(&s))
    }

    fn into_error(self) -> Error {
        match self.status {
            Some(s) if self.contract && REJECTED_INPUT.contains(&s) => invalid(self.reason),
            status if status.is_none_or(|s| TRANSIENT.contains(&s)) => Error::Unavailable(self.reason),
            _ => Error::Upstream(self.reason),
        }
    }
}

/// 网页搜索与抓取服务（DeepSeek）。出错怎么分类见 crate 文档。
pub struct DeepSeekWeb {
    client: reqwest::Client,
    base_url: String,
    authorization: String,
}

impl DeepSeekWeb {
    pub fn new(api_key: &str, base_url: &str, client: reqwest::Client) -> Self {
        Self {
            client,
            base_url: base_url.trim_end_matches('/').to_string(),
            authorization: format!("Bearer {api_key}"),
        }
    }

    async fn post<T: DeserializeOwned>(
        &self,
        op: &'static str,
        body: serde_json::Value,
    ) -> std::result::Result<T, Failure> {
        let failure = |status: Option<u16>, contract: bool, reason: String| {
            tracing::warn!(provider = "deepseek", op, %reason, "web request failed");
            Failure { status, contract, reason }
        };
        let transport = |e: reqwest::Error| failure(None, false, format!("deepseek {op} failed: {e}"));
        let resp = self
            .client
            .post(format!("{}/v1/tools/{op}", self.base_url))
            .header(reqwest::header::AUTHORIZATION, &self.authorization)
            .json(&body)
            .timeout(TIMEOUT)
            .send()
            .await
            .map_err(transport)?;
        let status = resp.status().as_u16();
        let bytes = resp.bytes().await.map_err(transport)?;
        if status == 200 {
            return serde_json::from_slice(&bytes).map_err(|e| {
                failure(
                    Some(status),
                    false,
                    format!("deepseek {op} returned an unexpected response ({e}): {}", preview(&bytes)),
                )
            });
        }
        let message = serde_json::from_slice::<ErrorBody>(&bytes).ok().map(|b| b.error.message);
        let detail = match &message {
            Some(m) => m.clone(),
            None => format!("response outside the error contract: {}", preview(&bytes)),
        };
        Err(failure(Some(status), message.is_some(), format!("deepseek {op} failed: HTTP {status}: {detail}")))
    }
}

fn preview(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).chars().take(500).collect()
}

impl DeepSeekWeb {
    pub async fn search(&self, query: &str, limit: u32) -> Result<Vec<SearchHit>> {
        let resp: SearchResponse = self
            .post("search", serde_json::json!({"query": query, "count": limit}))
            .await
            .map_err(Failure::into_error)?;
        tracing::info!(provider = "deepseek", count = resp.results.len(), "web search");
        Ok(resp
            .results
            .into_iter()
            .map(|r| SearchHit {
                title: r.title,
                url: r.url,
                summary: r.summary,
                site_name: r.site_name,
                published: r.date,
            })
            .collect())
    }

    pub async fn fetch(&self, url: &str) -> Result<Page> {
        let failure = match self.post::<FetchResponse>("fetch", serde_json::json!({"url": url})).await {
            Ok(page) => {
                tracing::info!(provider = "deepseek", host = %host_of(url), bytes = page.content.len(), "web fetch");
                return Ok(Page { title: page.title, content: page.content });
            }
            Err(f) => f,
        };
        // 服务说目标抓不到，或服务本身暂时不可用：都直连一次作备用（#309）。400/413（URL 本身无效）与 key、余额
        // 等配置问题不在此列，照旧交还模型或让这一轮失败
        if failure.target_unavailable() {
            return direct::fetch(&self.client, url, &failure.reason).await;
        }
        match failure.into_error() {
            e if e.is_transient() => direct::fetch(&self.client, url, &e.to_string()).await,
            other => Err(other),
        }
    }
}
