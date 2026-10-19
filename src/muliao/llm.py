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

from muliao.config import LlmConfig, ProviderConfig
from muliao.errors import InvalidInput

ModelResolver = Callable[[str], Model]


def _build_provider(cfg: ProviderConfig) -> Provider:  # type: ignore[type-arg]
    key = cfg.api_key.get_secret_value()
    match cfg.type:
        case "anthropic":
            return AnthropicProvider(api_key=key, base_url=cfg.base_url)
        case "openai" | "openai-responses":
            return OpenAIProvider(api_key=key, base_url=cfg.base_url)
        case "google":
            return GoogleProvider(api_key=key, base_url=cfg.base_url)
        case _:
            assert_never(cfg.type)


class ModelRegistry:
    def __init__(self, config: LlmConfig):
        self._config = config
        # 每个 provider 一个客户端，在所有 agent 间复用连接池
        self._providers: dict[str, Provider] = {}  # type: ignore[type-arg]

    def _split(self, model: str) -> tuple[str, ProviderConfig, str]:
        name, sep, model_name = model.partition(":")
        if not sep or not model_name:
            raise InvalidInput(f"model {model!r} must be '<provider>:<model>'")
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
