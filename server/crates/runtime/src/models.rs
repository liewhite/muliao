//! 按配置构造模型。agent 的 model 字段格式为 `<provider 名>:<模型名>`，provider 名是配置 `[llm.providers.<名>]` 的键。
//! 密钥与地址只来自配置文件。每个 provider 一个并发上限，这个 provider 下所有模型、所有 agent 共用。
//!
//! 和 Python 版不同：不再按模型名查能力表（pydantic-ai 的 profile）判断思考档位能不能用，档位总是照发，模型不支持时由服务端
//! 报错（agenter 的做法，见它的 README）。所以「这个模型能不能运行」在这里只查得到 provider 配了没有、名字格式对不对。

use std::collections::BTreeMap;
use std::sync::Arc;

use agenter::anthropic::AnthropicModel;
use agenter::openai_responses::OpenAIResponsesModel;
use agenter::{Effort, Limit, Model, ModelError};
use muliao_assets::formats::agent_file::Thinking;
use muliao_platform::config::{LlmConfig, ProviderType, split_model};
use muliao_platform::errors::DomainError;

/// 各协议的官方地址：配置不写 base_url 时用（永远显式给地址，不让 SDK 读环境变量）。
fn official_base_url(kind: ProviderType) -> &'static str {
    match kind {
        // agenter 的 Anthropic 地址不带 /v1
        ProviderType::Anthropic => "https://api.anthropic.com",
        ProviderType::OpenAiResponses => "https://api.openai.com/v1",
    }
}

struct Provider {
    kind: ProviderType,
    api_key: String,
    base_url: String,
    prompt_cache: bool,
    limit: Limit,
}

pub struct ModelRegistry {
    providers: BTreeMap<String, Provider>,
    /// 所有模型请求共用的 HTTP 客户端（连接池在所有 agent 间复用；要设读超时，由组合根建）
    http: reqwest::Client,
}

/// agent 的思考档位换成模型层的。
pub fn thinking_of(t: Thinking) -> agenter::Thinking {
    match t {
        Thinking::Default => agenter::Thinking::Default,
        Thinking::Off => agenter::Thinking::Off,
        Thinking::Low => agenter::Thinking::Effort(Effort::Low),
        Thinking::Medium => agenter::Thinking::Effort(Effort::Medium),
        Thinking::High => agenter::Thinking::Effort(Effort::High),
        Thinking::Xhigh => agenter::Thinking::Effort(Effort::XHigh),
    }
}

impl ModelRegistry {
    pub fn new(config: &LlmConfig, http: reqwest::Client) -> Self {
        let providers = config
            .providers
            .iter()
            .map(|(name, cfg)| {
                let provider = Provider {
                    kind: cfg.kind,
                    api_key: cfg.api_key.expose().to_owned(),
                    base_url: cfg.base_url.clone().unwrap_or_else(|| official_base_url(cfg.kind).to_owned()),
                    prompt_cache: cfg.prompt_cache(),
                    limit: Limit::new(format!("provider:{name}"), cfg.max_concurrency),
                };
                (name.clone(), provider)
            })
            .collect();
        Self { providers, http }
    }

    fn split<'a>(&self, model: &'a str) -> Result<(&Provider, &'a str), DomainError> {
        let (name, model_name) = split_model(model).map_err(DomainError::invalid)?;
        let provider = self.providers.get(name).ok_or_else(|| {
            DomainError::invalid(format!(
                "model {model:?}: provider {name:?} is not configured, have {:?}",
                self.provider_names()
            ))
        })?;
        Ok((provider, model_name))
    }

    /// agent 的模型在当前配置下能不能运行（新建、修改与读资源树时，资源目录的 ModelCheck）。不访问网络。
    pub fn check(&self, model: &str) -> Result<(), DomainError> {
        self.split(model).map(drop)
    }

    /// 交给资源目录的模型检查（组合根装配时用）。
    pub fn model_check(self: &Arc<Self>) -> muliao_assets::source::ModelCheck {
        let models = self.clone();
        Arc::new(move |model: &str, _thinking| models.check(model).map_err(|e| e.message))
    }

    pub fn provider_names(&self) -> Vec<String> {
        self.providers.keys().cloned().collect()
    }

    /// 所有模型调用（会话运行、压缩）都经这里拿到模型，并发上限在这里统一套上。
    pub fn resolve(&self, model: &str) -> Result<Arc<dyn Model>, DomainError> {
        let (p, name) = self.split(model)?;
        let http = self.http.clone();
        Ok(match p.kind {
            ProviderType::Anthropic => Arc::new(
                p.limit.wrap(AnthropicModel::new(http, &p.base_url, &p.api_key, name).prompt_cache(p.prompt_cache)),
            ),
            ProviderType::OpenAiResponses => {
                Arc::new(p.limit.wrap(OpenAIResponsesModel::new(http, &p.base_url, &p.api_key, name)))
            }
        })
    }

    /// 向这个 provider 实时查询它提供的模型（界面上的同步等待）；失败原样报出，不用猜测的列表兜底。
    pub async fn list_models(&self, provider_name: &str) -> Result<Vec<String>, ListError> {
        let p = self.providers.get(provider_name).ok_or_else(|| {
            DomainError::not_found(format!(
                "provider {provider_name:?} is not configured, have {:?}",
                self.provider_names()
            ))
        })?;
        let listed = match p.kind {
            ProviderType::Anthropic => agenter::anthropic::list_models(&self.http, &p.base_url, &p.api_key).await,
            ProviderType::OpenAiResponses => {
                agenter::openai_responses::list_models(&self.http, &p.base_url, &p.api_key).await
            }
        };
        match listed {
            Ok(mut names) => {
                names.sort();
                tracing::info!(provider = provider_name, count = names.len(), "listed models");
                Ok(names)
            }
            Err(e) => {
                tracing::warn!(provider = provider_name, error = %e, "listing models failed");
                Err(ListError::Upstream { provider: provider_name.to_owned(), source: e })
            }
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ListError {
    #[error(transparent)]
    Domain(#[from] DomainError),
    /// provider 那边出错（鉴权、限流、5xx、网络不可达）
    #[error("provider {provider:?} failed to list models: {source}")]
    Upstream { provider: String, source: ModelError },
}

#[cfg(test)]
mod tests {
    use muliao_platform::config::Settings;

    use super::*;

    fn registry() -> ModelRegistry {
        let text = r#"
            [server]
            public_url = "http://127.0.0.1:1080"
            [auth]
            password = "12345678"
            secret_key = "0123456789abcdef0123456789abcdef"
            [vault]
            key = "k"
            [push]
            vapid_subject = "mailto:a@b.c"
            [llm]
            default_model = "claude:m"
            [llm.providers.claude]
            type = "anthropic"
            api_key = "k1"
            max_concurrency = 1
            [llm.providers.ds]
            type = "openai-responses"
            api_key = "k2"
            base_url = "http://127.0.0.1:1/v1"
        "#;
        let s = Settings::parse(text, "/tmp/c.toml".into()).unwrap();
        ModelRegistry::new(&s.llm, reqwest::Client::new())
    }

    #[test]
    fn resolves_configured_providers_and_reports_the_rest() {
        let r = registry();
        assert_eq!(r.provider_names(), ["claude", "ds"]);
        assert_eq!(r.resolve("claude:claude-x").unwrap().name(), "claude-x");
        assert_eq!(r.resolve("ds:deepseek-v9").unwrap().name(), "deepseek-v9");
        let e = r.check("nope:x").unwrap_err();
        assert!(e.message.contains("provider \"nope\" is not configured"), "{e}");
        assert!(r.check("no-colon").is_err());
    }

    #[tokio::test]
    async fn listing_an_unknown_provider_is_not_found() {
        let e = registry().list_models("nope").await.unwrap_err();
        assert!(matches!(e, ListError::Domain(d) if d.kind == muliao_platform::errors::ErrorKind::NotFound));
    }
}
