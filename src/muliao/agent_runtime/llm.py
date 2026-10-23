"""按配置构造模型。agent 的 model 字段格式为 "<provider 名>:<模型名>"，provider 名是配置中
`[llm.providers.<名>]` 的键。密钥与地址只来自配置文件。"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic
import httpx2
import openai
from google.genai import errors as google_errors
from google.genai import types as google_types
from pydantic_ai.concurrency import AbstractConcurrencyLimiter, ConcurrencyLimiter
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers import Provider
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from muliao.agents.api import Thinking
from muliao.config import LlmConfig, ProviderConfig, ProviderType, split_model
from muliao.errors import InvalidInput, NotFound, UpstreamFailed

log = logging.getLogger(__name__)

ModelResolver = Callable[[str], Model]


# Anthropic 的 prompt caching（https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching）：
# 在每次请求都原样重发的前缀上放缓存断点——指令（instructions，含全局记忆、记忆、skill 目录）、工具定义，
# 以及消息末尾（让已经发过的历史在下一次请求里命中缓存）。三个断点，不超过 Anthropic 一次请求最多 4 个的上限
# （超出的断点 pydantic_ai.models.anthropic._limit_cache_points 会按从旧到新剔除，我们不依赖它）。
# 用 anthropic_cache_messages 而不是服务端自动放断点的 anthropic_cache：type = "anthropic" 也用于 Anthropic
# 兼容的网关，它们不一定支持顶层的自动缓存参数，而逐块的 cache_control 是协议本身的一部分。
ANTHROPIC_CACHE_SETTINGS = AnthropicModelSettings(
    anthropic_cache_instructions=True,
    anthropic_cache_tool_definitions=True,
    anthropic_cache_messages=True,
)


def model_settings(thinking: Thinking) -> ModelSettings | None:
    """agent 的思考档位对应的运行级 model settings。"default" 什么都不传；其余原样交给 pydantic_ai 统一的
    ModelSettings.thinking（False 或 effort 档位），由它换算成各 provider 的参数（Anthropic 的 adaptive + effort、
    OpenAI 兼容接口的 reasoning_effort 等），服务端不接受时如实报错。pydantic_ai 把运行级设置合并在模型自带的设置
    （ANTHROPIC_CACHE_SETTINGS）之上，两者的键不重叠，互不覆盖。用之前先 check_thinking。"""
    if thinking == "default":
        return None
    return ModelSettings(thinking=False if thinking == "off" else thinking)


def check_thinking(name: str, model: Model, thinking: Thinking) -> None:
    """模型 name（"<provider 名>:<模型名>"，model 是它解析出的模型）能否按这个思考档位运行。判断依据与 pydantic_ai 的
    Model.prepare_request 相同，但它在不满足时（profile 不支持思考；总在思考的模型上的 off）只是悄悄丢掉设置，
    这里报 InvalidInput：设置不生效就如实说不生效。
    profile 由 pydantic_ai 按 provider 类型与模型名推断：经 OpenAI 兼容接口接入的其他厂商模型（如 deepseek）按 OpenAI 的
    模型名规则判断，可能被判为不支持。"""
    if thinking == "default":
        return
    profile = model.profile
    always = profile.get("thinking_always_enabled", False)
    if not (profile.get("supports_thinking", False) or always):
        raise InvalidInput(
            f"model {name!r} does not take a thinking setting (per its pydantic_ai profile), "
            f"only thinking 'default' works with it"
        )
    if thinking == "off" and always:
        raise InvalidInput(f"model {name!r} always thinks, thinking cannot be 'off'")


# 查询模型列表时 provider 按其契约可能出现的失败：鉴权、限流、5xx、网络不可达。
# anthropic / openai 把网络错误包成自己的 APIError；google 不包，直接抛其 HTTP 客户端（httpx2）的错误
_LIST_ERRORS = (anthropic.APIError, openai.APIError, google_errors.APIError, httpx2.HTTPError)
# 列表查询是界面上的同步等待：不沿用推理请求长达十几分钟的超时，provider 挂起时尽快报错
LIST_TIMEOUT_SECONDS = 30.0


async def _sdk_model_names(provider: Provider) -> list[str]:  # type: ignore[type-arg]
    # anthropic 与 openai 两个 SDK 的分页器都会自动翻完所有页
    return [m.id async for m in provider.client.models.list(timeout=LIST_TIMEOUT_SECONDS)]


async def _google_model_names(provider: Provider) -> list[str]:  # type: ignore[type-arg]
    config = google_types.ListModelsConfig(
        http_options=google_types.HttpOptions(timeout=int(LIST_TIMEOUT_SECONDS * 1000))
    )
    names = []
    async for m in await provider.client.aio.models.list(config=config):
        # 配置只能指向 Gemini API（api_key + base_url，不是 Vertex），它的 Model 总带 name 与
        # supportedGenerationMethods；缺了就是契约之外的响应，报错而不猜
        if m.name is None or m.supported_actions is None:
            raise RuntimeError(f"google listed a model without name or supported actions: {m!r}")
        # 只要能对话的模型（排除 embedding 等）
        if "generateContent" in m.supported_actions:
            names.append(m.name.removeprefix("models/"))
    return names


@dataclass(frozen=True)
class ProviderKind:
    """一种 provider 类型（配置里的 type）怎么接：官方地址、构造客户端、构造模型、查询模型名。"""

    # 各 SDK 在 base_url 为 None 时会读环境变量（ANTHROPIC_BASE_URL、OPENAI_BASE_URL、GOOGLE_GEMINI_BASE_URL）
    # 或本地 profile，可能把配置里的 key 发往别处。所以永远显式传地址。取值与各 SDK 内置默认一致。
    official_base_url: str
    # (api_key, base_url) → 客户端
    provider: Callable[[str, str], Provider]  # type: ignore[type-arg]
    # (模型名, 客户端, provider 配置) → 模型
    model: Callable[[str, Provider, ProviderConfig], Model]  # type: ignore[type-arg]
    # 向 provider 查询它提供的模型名（即 "<provider 名>:<模型名>" 里的模型名）
    model_names: Callable[[Provider], Awaitable[list[str]]]  # type: ignore[type-arg]


def _openai(api_key: str, base_url: str) -> Provider:  # type: ignore[type-arg]
    return OpenAIProvider(api_key=api_key, base_url=base_url)


# 每种 provider 类型一行；新增类型只在这里加（覆盖 ProviderType 的全部取值，由测试保证）
KINDS: dict[ProviderType, ProviderKind] = {
    "anthropic": ProviderKind(
        official_base_url="https://api.anthropic.com",
        provider=lambda key, url: AnthropicProvider(api_key=key, base_url=url),
        model=lambda name, provider, cfg: AnthropicModel(
            name, provider=provider, settings=ANTHROPIC_CACHE_SETTINGS if cfg.prompt_cache else None
        ),
        model_names=_sdk_model_names,
    ),
    "openai": ProviderKind(
        official_base_url="https://api.openai.com/v1",
        provider=_openai,
        model=lambda name, provider, _cfg: OpenAIChatModel(name, provider=provider),
        model_names=_sdk_model_names,
    ),
    "openai-responses": ProviderKind(
        official_base_url="https://api.openai.com/v1",
        provider=_openai,
        model=lambda name, provider, _cfg: OpenAIResponsesModel(name, provider=provider),
        model_names=_sdk_model_names,
    ),
    "google": ProviderKind(
        official_base_url="https://generativelanguage.googleapis.com/",
        provider=lambda key, url: GoogleProvider(api_key=key, base_url=url),
        model=lambda name, provider, _cfg: GoogleModel(name, provider=provider),
        model_names=_google_model_names,
    ),
}


class _ProviderLimiter(AbstractConcurrencyLimiter):
    """一个 provider 同时进行的模型请求上限（ProviderConfig.max_concurrency），记录排队：请求要等配额时记一条，
    拿到时记等了多久——慢是因为排队还是因为模型本身，从日志能分清。"""

    def __init__(self, provider: str, max_running: int):
        self._provider = provider
        self._limiter = ConcurrencyLimiter(max_running, name=f"provider:{provider}")

    async def acquire(self, source: str) -> None:
        if self._limiter.available_count:
            await self._limiter.acquire(source)
            return
        ctx = {"provider": self._provider, "source": source, "waiting": self._limiter.waiting_count + 1}
        log.info("model request waiting for a slot", extra=ctx)
        started = time.monotonic()
        await self._limiter.acquire(source)
        log.info(
            "model request got a slot",
            extra={**ctx, "waited_seconds": round(time.monotonic() - started, 1)},
        )

    def release(self) -> None:
        self._limiter.release()


class ModelRegistry:
    def __init__(self, config: LlmConfig):
        self._config = config
        # 每个 provider 一个客户端，在所有 agent 间复用连接池
        self._providers: dict[str, Provider] = {}  # type: ignore[type-arg]
        # 每个 provider 一个并发上限，这个 provider 下的所有模型共用
        self._limiters = {
            name: _ProviderLimiter(name, cfg.max_concurrency) for name, cfg in config.providers.items()
        }

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
            kind = KINDS[cfg.type]
            key = cfg.api_key.get_secret_value()
            provider = self._providers[name] = kind.provider(key, cfg.base_url or kind.official_base_url)
        return provider

    def check(self, model: str, thinking: Thinking) -> None:
        """agent 的模型与思考档位在当前配置下能否运行（新建、修改与启动检查时）。不访问网络。"""
        check_thinking(model, self.resolve(model), thinking)

    def provider_names(self) -> list[str]:
        return sorted(self._config.providers)

    async def list_models(self, provider_name: str) -> list[str]:
        """向这个 provider 实时查询它提供的模型；失败原样报出，不用猜测的列表兜底。"""
        cfg = self._config.providers.get(provider_name)
        if cfg is None:
            raise NotFound(f"provider {provider_name!r} is not configured, have {self.provider_names()}")
        try:
            names = await KINDS[cfg.type].model_names(self._provider(provider_name, cfg))
        except _LIST_ERRORS as e:
            log.warning("listing models failed", extra={"provider": provider_name, "error": repr(e)})
            raise UpstreamFailed(f"provider {provider_name!r} failed to list models: {e}") from e
        log.info("listed models", extra={"provider": provider_name, "count": len(names)})
        return sorted(names)

    def resolve(self, model: str) -> Model:
        """所有模型调用（会话运行、压缩、临时 agent）都经这里拿到模型，并发上限在这里统一套上。"""
        name, cfg, model_name = self._split(model)
        return ConcurrencyLimitedModel(
            KINDS[cfg.type].model(model_name, self._provider(name, cfg), cfg), self._limiters[name]
        )
