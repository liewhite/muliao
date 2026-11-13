//! 网页搜索与抓取（配置了 `[web]` 才有）。[`WebProvider`] 是 web 工具（[`tools`]）依赖的唯一接口，具体服务是它的实现，
//! 由配置 `[web].type` 选择（[`build`]）。网页内容是不可信输入（docs/architecture.md 第 15 节）。
//!
//! 各服务自己的输入限制（query 长度、条数上限、URL 规则）以服务端校验为唯一来源，这里不重复一份：服务端拒绝时把它的
//! 原因交还模型修正。

mod deepseek;
mod direct;
pub mod tools;

use std::sync::Arc;

use futures::future::BoxFuture;
use muliao_platform::config::{WebConfig, WebProviderType};
use muliao_platform::errors::Result;
use serde::{Deserialize, Serialize};

/// 交给模型的是它的 JSON；界面展示时从那段 JSON 还原（Deserialize）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SearchHit {
    pub title: String,
    pub url: String,
    /// 给模型读的摘要
    pub summary: String,
    pub site_name: Option<String>,
    /// 发布时间（RFC 3339），未知时为 None
    pub published: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Page {
    /// 没有标题时为空
    pub title: String,
    /// Markdown 正文；直连抓到纯文本时是原文
    pub content: String,
}

/// 搜索、抓取没有交回结果时按平台的错误分类（[`muliao_platform::errors::Error`]）报，由工具层按统一规则处理：
/// - 领域错误（InvalidInput）：输入被服务拒绝（模型改了再试），或页面经服务和直连都拿不到（原因里写着两边各自的原因，
///   由模型决定稍后重试、换 URL 还是改用浏览器）；交还模型。
/// - 暂时性的外部服务错误：服务暂时不可用（连不上、超时、429、5xx），稍后重试。
/// - 非暂时性的外部服务错误：配置或账户问题（key 无效、余额不足）、契约之外的响应，模型改不了，让这一轮失败可见。
pub trait WebProvider: Send + Sync {
    fn search<'a>(&'a self, query: &'a str, limit: u32) -> BoxFuture<'a, Result<Vec<SearchHit>>>;
    fn fetch<'a>(&'a self, url: &'a str) -> BoxFuture<'a, Result<Page>>;
}

/// 按配置选实现。`client` 是共用的出站 HTTP 客户端（由组合根创建；要跟随重定向，即 reqwest 的默认设置）。
pub fn build(cfg: &WebConfig, client: reqwest::Client) -> Arc<dyn WebProvider> {
    match cfg.kind {
        WebProviderType::Deepseek => Arc::new(deepseek::DeepSeekWeb::new(
            cfg.api_key.expose(),
            cfg.base_url.as_deref().unwrap_or(deepseek::BASE_URL),
            client,
        )),
    }
}

/// URL 的主机名，记日志用：完整 URL 里可能带着对话内容
fn host_of(url: &str) -> String {
    reqwest::Url::parse(url).ok().and_then(|u| u.host_str().map(str::to_string)).unwrap_or_default()
}
