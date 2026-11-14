//! 配置文件的 `[llm]` 段：模型服务（[`crate::models::ModelRegistry`] 按它连）与新建 agent 的默认模型。

use std::collections::BTreeMap;
use std::num::NonZeroUsize;

use muliao_platform::config::{ConfigSection, ModelRef, Secret, at_least};
use serde::Deserialize;

/// 模型接口的协议（agenter 支持的两种）。anthropic 总是放 prompt caching 的缓存断点，不设开关（#824）
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub enum Protocol {
    #[serde(rename = "anthropic")]
    Anthropic,
    #[serde(rename = "openai-responses")]
    OpenAiResponses,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderConfig {
    #[serde(rename = "type")]
    pub protocol: Protocol,
    pub api_key: Secret,
    /// 为空时用该协议的官方地址；OpenAI 兼容服务填其地址
    pub base_url: Option<String>,
    /// 同时进行的模型请求上限（这个 provider 下所有模型、所有 agent 共用；流式请求在整个流期间占一个）
    #[serde(default = "default_max_concurrency")]
    pub max_concurrency: NonZeroUsize,
}

fn default_max_concurrency() -> NonZeroUsize {
    const { NonZeroUsize::new(4).unwrap() }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LlmConfig {
    /// 新建 agent 时的默认模型，格式 "<provider 名>:<模型名>"
    pub default_model: ModelRef,
    pub providers: BTreeMap<String, ProviderConfig>,
}

impl ConfigSection for LlmConfig {
    const NAME: &'static str = "llm";

    fn check(&self) -> Vec<String> {
        let mut e = Vec::new();
        if self.providers.is_empty() {
            e.push("llm.providers must have at least one provider".into());
        }
        for (name, p) in &self.providers {
            at_least(&mut e, &format!("llm.providers.{name}.api_key length"), p.api_key.expose().len(), 1);
        }
        let provider = self.default_model.provider();
        if !self.providers.contains_key(provider) {
            e.push(format!(
                "llm.default_model provider {provider:?} is not in {:?}",
                self.providers.keys().collect::<Vec<_>>()
            ));
        }
        e
    }
}
