//! 配置文件的 `[web]` 段（组合根取出，[`crate::build`] 按它选服务）。

use muliao_platform::config::{ConfigSection, Secret, at_least};
use serde::Deserialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WebProviderType {
    Deepseek,
}

/// 网页搜索与抓取服务。不配置时没有 web 工具组。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WebConfig {
    #[serde(rename = "type")]
    pub kind: WebProviderType,
    pub api_key: Secret,
    /// 为空时用该类型的官方地址
    pub base_url: Option<String>,
}

impl ConfigSection for WebConfig {
    const NAME: &'static str = "web";

    fn check(&self) -> Vec<String> {
        let mut e = Vec::new();
        at_least(&mut e, "web.api_key length", self.api_key.expose().len(), 1);
        e
    }
}
