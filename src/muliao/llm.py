"""按配置构造模型。agent 的 model 字段格式为 "<provider 名>:<模型名>"，provider 名是配置中
`[llm.providers.<名>]` 的键。密钥与地址只来自配置文件。"""

from collections.abc import Callable
from typing import assert_never

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers import Provider
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from muliao.config import LlmConfig, ProviderConfig, ProviderType, split_model
from muliao.errors import InvalidInput

ModelResolver = Callable[[str], Model]


# 各 SDK 在 base_url 为 None 时会读环境变量（ANTHROPIC_BASE_URL、OPENAI_BASE_URL、GOOGLE_GEMINI_BASE_URL）
# 或本地 profile，可能把配置里的 key 发往别处。所以永远显式传地址。取值与各 SDK 内置默认一致。
OFFICIAL_BASE_URLS: dict[ProviderType, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "openai-responses": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com/",
}


def _build_provider(cfg: ProviderConfig) -> Provider:  # type: ignore[type-arg]
    key = cfg.api_key.get_secret_value()
    base_url = cfg.base_url or OFFICIAL_BASE_URLS[cfg.type]
    match cfg.type:
        case "anthropic":
            return AnthropicProvider(api_key=key, base_url=base_url)
        case "openai" | "openai-responses":
            return OpenAIProvider(api_key=key, base_url=base_url)
        case "google":
            return GoogleProvider(api_key=key, base_url=base_url)
        case _:
            assert_never(cfg.type)


class ModelRegistry:
    def __init__(self, config: LlmConfig):
        self._config = config
        # 每个 provider 一个客户端，在所有 agent 间复用连接池
        self._providers: dict[str, Provider] = {}  # type: ignore[type-arg]

    def _split(self, model: str) -> tuple[str, ProviderConfig, str]:
        try:
            name, model_name = split_model(model)
        except ValueError as e:
            raise InvalidInput(str(e)) from e
        cfg = self._config.providers.get(name)
        if cfg is None:
            raise InvalidInput(
                f"model {model!r}: provider {name!r} is not configured, have {sorted(self._config.providers)}"
            )
        return name, cfg, model_name

    def check(self, model: str) -> None:
        self._split(model)

    def resolve(self, model: str) -> Model:
        name, cfg, model_name = self._split(model)
        provider = self._providers.get(name)
        if provider is None:
            provider = self._providers[name] = _build_provider(cfg)
        match cfg.type:
            case "anthropic":
                return AnthropicModel(model_name, provider=provider)
            case "openai":
                return OpenAIChatModel(model_name, provider=provider)
            case "openai-responses":
                return OpenAIResponsesModel(model_name, provider=provider)
            case "google":
                return GoogleModel(model_name, provider=provider)
            case _:
                assert_never(cfg.type)
