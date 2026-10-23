"""agent 的思考档位（#171）：交给 pydantic_ai 统一的 ModelSettings.thinking 换算，与 prompt cache 设置并存；
模型会悄悄忽略的档位如实拒绝。"""

import pytest
from pydantic import SecretStr
from pydantic_ai.settings import ModelSettings

from muliao.agent_runtime import llm
from muliao.config import LlmConfig, ProviderConfig
from muliao.errors import InvalidInput
from tests.test_prompt_cache import _cache_points, _request_body


def test_default_sends_no_settings_and_off_disables():
    assert llm.model_settings("default") is None
    assert llm.model_settings("off") == {"thinking": False}
    assert llm.model_settings("xhigh") == {"thinking": "xhigh"}


async def test_anthropic_request_carries_effort_alongside_cache_points():
    # max_tokens 取小值：这个模型默认的输出上限会让 SDK 改走流式，模拟的非流式回复就对不上了
    small = ModelSettings(max_tokens=1024)
    plain = await _request_body(prompt_cache=None, model="claude-opus-5-5", settings=small)
    assert "thinking" not in plain and "output_config" not in plain  # 默认：与之前一样什么都不传

    body = await _request_body(
        prompt_cache=None, model="claude-opus-5-5", settings={**small, **(llm.model_settings("high") or {})}
    )
    # pydantic_ai 把档位换算成 Anthropic 的 adaptive thinking + effort；缓存断点不受影响
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert _cache_points(body) == _cache_points(plain) == 3


def test_registry_rejects_levels_the_model_would_silently_drop():
    config = LlmConfig(
        default_model="a:claude-opus-5-5",
        providers={
            "a": ProviderConfig(type="anthropic", api_key=SecretStr("k")),
            "o": ProviderConfig(type="openai-responses", api_key=SecretStr("k"), base_url="https://x/v1"),
        },
    )
    registry = llm.ModelRegistry(config)
    registry.check("a:claude-opus-5-5", "low")
    registry.check("o:gpt-5.5", "off")
    with pytest.raises(InvalidInput, match="always thinks"):
        registry.check("a:claude-opus-5-5", "off")
    # 经 OpenAI 兼容接口接入的 deepseek 按 OpenAI 的模型名规则推断 profile：不支持思考设置
    with pytest.raises(InvalidInput, match="does not take a thinking setting"):
        registry.check("o:deepseek-v4-flash", "high")
    registry.check("o:deepseek-v4-flash", "default")
