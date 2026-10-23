"""Anthropic 的 prompt caching：默认在指令、工具定义与消息末尾放缓存断点，provider 的 prompt_cache 可以关掉。"""

import json

import httpx2
import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai import Agent as PaiAgent
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.settings import ModelSettings

from muliao.agent_runtime import llm
from muliao.config import LlmConfig, ProviderConfig

_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 2},
}


async def _request_body(
    *, prompt_cache: bool | None, model: str = "m", settings: ModelSettings | None = None
) -> dict:
    """用真实的 Anthropic SDK 与模型实现发一次请求（settings 是运行级 model settings），返回发到线上的请求体。"""
    sent: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=_REPLY)

    options = {} if prompt_cache is None else {"prompt_cache": prompt_cache}
    config = LlmConfig(
        default_model="a:m",
        providers={"a": ProviderConfig(type="anthropic", api_key=SecretStr("k"), **options)},
    )
    registry = llm.ModelRegistry(config)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        # 换掉连向真实地址的客户端（同 test_provider_models）：走完整的 SDK 与模型实现，断言真正发出的请求体
        registry._providers["a"] = AnthropicProvider(  # noqa: SLF001
            api_key="k", base_url="https://api.example", http_client=http
        )
        agent = PaiAgent(model=registry.resolve(f"a:{model}"), instructions="Be helpful.")

        @agent.tool_plain
        def look(query: str) -> str:
            """Look something up."""
            return query

        await agent.run("hi", model_settings=settings)
    assert len(sent) == 1
    return sent[0]


def _cache_points(body: dict) -> int:
    """请求体里 cache_control 的个数（Anthropic 一次请求最多 4 个）。"""
    return json.dumps(body).count('"cache_control"')


async def test_enabled_by_default_the_request_caches_instructions_tools_and_messages():
    body = await _request_body(prompt_cache=None)

    assert "cache_control" in body["system"][-1]
    assert "cache_control" in body["tools"][-1]
    assert "cache_control" in body["messages"][-1]["content"][-1]
    # 三个断点，不超过 Anthropic 的上限
    assert _cache_points(body) == 3


async def test_turning_prompt_cache_off_sends_no_cache_control():
    body = await _request_body(prompt_cache=False)

    assert _cache_points(body) == 0
    # 关掉的只是缓存，请求本身照常带指令与工具
    assert body["tools"] and body["system"]


def test_prompt_cache_on_a_non_anthropic_provider_is_a_config_error():
    with pytest.raises(ValidationError, match="prompt_cache"):
        ProviderConfig(type="openai", api_key=SecretStr("k"), prompt_cache=False)


def test_other_provider_types_stay_valid_without_the_switch():
    # 不填就不是误解：别的类型照常构造（它们的实现从不读这个字段）
    ProviderConfig(type="openai", api_key=SecretStr("k"))
