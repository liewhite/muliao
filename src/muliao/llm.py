"""按配置构造模型。agent 的 model 字段格式为 "<provider 名>:<模型名>"，provider 名是配置中
`[llm.providers.<名>]` 的键。密钥与地址只来自配置文件。"""

import logging
from collections.abc import Callable
from typing import assert_never

import anthropic
import httpx2
import openai
from google.genai import errors as google_errors
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers import Provider
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from muliao.config import LlmConfig, ProviderConfig, ProviderType, split_model
from muliao.errors import InvalidInput, NotFound, UpstreamFailed

log = logging.getLogger(__name__)

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


# 查询模型列表时 provider 按其契约可能出现的失败：鉴权、限流、5xx、网络不可达。
# anthropic / openai 把网络错误包成自己的 APIError；google 不包，直接抛其 HTTP 客户端（httpx2）的错误
_LIST_ERRORS = (anthropic.APIError, openai.APIError, google_errors.APIError, httpx2.HTTPError)


async def _fetch_model_names(kind: ProviderType, provider: Provider) -> list[str]:  # type: ignore[type-arg]
    """向 provider 查询它提供的模型名（即 "<provider 名>:<模型名>" 里的模型名）。"""
    match kind:
        case "anthropic" | "openai" | "openai-responses":
            # 两个 SDK 的分页器都会自动翻完所有页
            return [m.id async for m in provider.client.models.list()]
        case "google":
            names = []
            async for m in await provider.client.aio.models.list():
                if m.name is None:
                    raise RuntimeError(f"google listed a model without a name: {m!r}")
                # 只要能对话的模型（排除 embedding 等）；接口没给出能力时无法判断，保留
                if m.supported_actions is None or "generateContent" in m.supported_actions:
                    names.append(m.name.removeprefix("models/"))
            return names
        case _:
            assert_never(kind)


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

    def _provider(self, name: str, cfg: ProviderConfig) -> Provider:  # type: ignore[type-arg]
        provider = self._providers.get(name)
        if provider is None:
            provider = self._providers[name] = _build_provider(cfg)
        return provider

    def check(self, model: str) -> None:
        self._split(model)

    def provider_names(self) -> list[str]:
        return sorted(self._config.providers)

    async def list_models(self, provider_name: str) -> list[str]:
        """向这个 provider 实时查询它提供的模型；失败原样报出，不用猜测的列表兜底。"""
        cfg = self._config.providers.get(provider_name)
        if cfg is None:
            raise NotFound(f"provider {provider_name!r} is not configured, have {self.provider_names()}")
        try:
            names = await _fetch_model_names(cfg.type, self._provider(provider_name, cfg))
        except _LIST_ERRORS as e:
            log.warning("listing models failed", extra={"provider": provider_name, "error": repr(e)})
            raise UpstreamFailed(f"provider {provider_name!r} failed to list models: {e}") from e
        log.info("listed models", extra={"provider": provider_name, "count": len(names)})
        return sorted(names)

    def resolve(self, model: str) -> Model:
        name, cfg, model_name = self._split(model)
        provider = self._provider(name, cfg)
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
