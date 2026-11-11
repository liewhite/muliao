"""agent 的思考档位（#171）：交给 pydantic_ai 统一的 ModelSettings.thinking 换算，与 prompt cache 设置并存；
模型会悄悄忽略的档位如实拒绝。"""

import json

import httpx2
import pytest
from pydantic import SecretStr
from pydantic_ai import Agent as PaiAgent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from muliao.agent_runtime import llm
from muliao.config import LlmConfig, ProviderConfig
from muliao.errors import InvalidInput
from tests.test_prompt_cache import _cache_points, _request_body

# max_tokens 取小值：这些模型默认的输出上限会让 SDK 改走流式，模拟的非流式回复就对不上了
_SMALL = ModelSettings(max_tokens=1024)
_SHOWN = {"type": "adaptive", "display": "summarized"}
_CHAT_REPLY = {
    "id": "c1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-5.5",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_RESPONSES_REPLY = {
    "id": "resp_1",
    "object": "response",
    "created_at": 0,
    "model": "gpt-5.5",
    "status": "completed",
    "output": [
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
        }
    ],
    "parallel_tool_calls": True,
    "tool_choice": "auto",
    "tools": [],
    "usage": {
        "input_tokens": 1,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 1,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 2,
    },
}


def _registry() -> llm.ModelRegistry:
    return llm.ModelRegistry(
        LlmConfig(
            default_model="a:claude-opus-5-5",
            providers={
                "a": ProviderConfig(type="anthropic", api_key=SecretStr("k")),
                "o": ProviderConfig(type="openai-responses", api_key=SecretStr("k"), base_url="https://x/v1"),
                "c": ProviderConfig(type="openai", api_key=SecretStr("k"), base_url="https://x/v1"),
            },
        )
    )


def _settings(model: str, thinking: llm.Thinking) -> ModelSettings | None:
    return llm.model_settings(_registry().resolve(model), thinking)


def test_level_goes_to_the_unified_setting_and_off_disables():
    # 没有「要回摘要」开关的模型（deepseek 按 OpenAI 的模型名规则推断成不思考）：默认什么都不传
    assert _settings("o:deepseek-v4-flash", "default") is None
    assert _settings("o:gpt-5.5", "off") == {"thinking": False}
    assert _settings("a:claude-opus-5", "off") == {"thinking": False}
    assert _settings("a:claude-opus-5-5", "xhigh") == {"thinking": "xhigh", "anthropic_thinking": _SHOWN}


def test_thinking_summaries_are_asked_for_where_the_model_thinks():
    """会话页展示的思考是提供方给的摘要（#603）：不要的话 Claude Opus 4.7 起只回签名、OpenAI 的推理模型什么都不回。
    只在这次请求会思考时要：给了档位，或 default 而模型不传也在思考。"""
    # 不传 thinking 也在思考的 Claude：要摘要
    assert _settings("a:claude-opus-5-5", "default") == {"anthropic_thinking": _SHOWN}
    assert _settings("a:claude-haiku-5-5", "default") == {"anthropic_thinking": _SHOWN}
    # 默认不思考的 Claude：default 不去打开思考；给了档位（pydantic_ai 本来就发 adaptive）才带
    assert _settings("a:claude-opus-4-8", "default") is None
    assert _settings("a:claude-opus-4-8", "low") == {"thinking": "low", "anthropic_thinking": _SHOWN}
    # 只支持 budget 的旧模型默认就回摘要，照旧只传档位
    assert _settings("a:claude-opus-4-5", "high") == {"thinking": "high"}
    # OpenAI Responses 的推理模型
    assert _settings("o:gpt-5.5", "default") == {"openai_reasoning_summary": "auto"}
    assert _settings("o:gpt-5.5", "high") == {"thinking": "high", "openai_reasoning_summary": "auto"}
    # 默认不推理的 OpenAI 模型：同样 default 不带、给了档位才带
    assert _settings("o:gpt-5.2", "default") is None
    assert _settings("o:gpt-5.2", "low") == {"thinking": "low", "openai_reasoning_summary": "auto"}
    assert _settings("o:gpt-4o", "default") is None


async def test_anthropic_request_asks_for_summaries_and_carries_effort_alongside_cache_points():
    plain = await _request_body(prompt_cache=None, model="claude-opus-5-5", settings=_SMALL)
    assert "thinking" not in plain and "output_config" not in plain  # pydantic_ai 自己什么都不传

    default = await _request_body(
        prompt_cache=None, model="claude-opus-5-5", settings=_SMALL, thinking="default"
    )
    # 默认档位：只多了「要摘要」，不带 effort
    assert default["thinking"] == _SHOWN
    assert "output_config" not in default

    body = await _request_body(prompt_cache=None, model="claude-opus-5-5", settings=_SMALL, thinking="high")
    # pydantic_ai 把档位换算成 effort；thinking 对象是我们给的 adaptive + 要摘要；缓存断点不受影响
    assert body["thinking"] == _SHOWN
    assert body["output_config"] == {"effort": "high"}
    assert _cache_points(body) == _cache_points(default) == _cache_points(plain) == 3

    off = await _request_body(prompt_cache=None, model="claude-opus-5", settings=_SMALL, thinking="off")
    assert off["thinking"] == {"type": "disabled"}


async def _openai_request_body(model: str, thinking: llm.Thinking, reply: dict) -> dict:
    """用真实的 OpenAI SDK 与模型实现按这个思考档位发一次请求（model 是 "<o 或 c>:<模型名>"），返回发到线上的请求体。"""
    sent: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=reply)

    registry = _registry()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        registry._providers[model.split(":")[0]] = OpenAIProvider(  # noqa: SLF001
            api_key="k", base_url="https://api.example/v1", http_client=http
        )
        resolved = registry.resolve(model)
        await PaiAgent(model=resolved).run("hi", model_settings=llm.model_settings(resolved, thinking))
    assert len(sent) == 1
    return sent[0]


async def test_openai_responses_request_asks_for_a_reasoning_summary():
    body = await _openai_request_body("o:gpt-5.5", "high", _RESPONSES_REPLY)
    assert body["reasoning"]["effort"] == "high"
    assert body["reasoning"]["summary"] == "auto"

    default = await _openai_request_body("o:gpt-5.5", "default", _RESPONSES_REPLY)
    assert default["reasoning"]["summary"] == "auto" and "effort" not in default["reasoning"]


async def test_chat_completions_request_carries_no_summary_setting():
    # profile 分不出接口，Chat Completions 接法也拿到要摘要的键：那个接口没有这项设置，不能出现在请求里
    body = await _openai_request_body("c:gpt-5.5", "high", _CHAT_REPLY)
    assert body["reasoning_effort"] == "high"
    assert "summary" not in json.dumps(body) and "reasoning" not in body


async def test_thinking_anthropic_cannot_take_back_is_left_out_of_the_request():
    """没有签名的思考（生成到一半被停止）和别家模型的思考，pydantic_ai 会改成 <thinking> 文字发出去，claude-fable-5-2
    因此整次拒答（#603）：发出去的历史里不带它们，带签名的本家思考与 redacted_thinking 原样传回。"""

    def reply(*parts, **kw) -> ModelResponse:
        return ModelResponse(
            parts=list(parts), provider_name="anthropic", model_name="claude-fable-5-2", **kw
        )

    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("q1")]),
        reply(
            ThinkingPart("signed", signature="sig", provider_name="anthropic"),
            ThinkingPart("", id="redacted_thinking", signature="data", provider_name="anthropic"),
            TextPart("a1"),
        ),
        ModelRequest(parts=[UserPromptPart("q2")]),
        ModelResponse(
            parts=[
                ThinkingPart("foreign", id="rs_1", signature="enc", provider_name="openai"),
                TextPart("a2"),
            ],
            provider_name="openai",
            model_name="gpt-5.5",
        ),
        ModelRequest(parts=[UserPromptPart("q3")]),
        reply(ThinkingPart("cut off half", provider_name="anthropic"), state="interrupted"),
    ]
    body = await _request_body(
        prompt_cache=None, model="claude-fable-5-2", settings=_SMALL, thinking="default", history=history
    )

    sent = json.dumps(body["messages"])
    assert "cut off half" not in sent and "foreign" not in sent and "<thinking>" not in sent
    blocks = [b for m in body["messages"] if m["role"] == "assistant" for b in m["content"]]
    assert {"type": "thinking", "thinking": "signed", "signature": "sig"} in blocks
    assert {"type": "redacted_thinking", "data": "data"} in blocks
    assert [b["text"] for b in blocks if b["type"] == "text"] == ["a1", "a2"]
    # 交进来的历史没被改动：库里与会话页照常保留这些思考
    assert isinstance(history[-1], ModelResponse) and history[-1].parts[0].content == "cut off half"


def test_registry_rejects_levels_the_model_would_silently_drop():
    registry = _registry()
    registry.check("a:claude-opus-5-5", "low")
    registry.check("o:gpt-5.5", "off")
    with pytest.raises(InvalidInput, match="always thinks"):
        registry.check("a:claude-opus-5-5", "off")
    # 经 OpenAI 兼容接口接入的 deepseek 按 OpenAI 的模型名规则推断 profile：不支持思考设置
    with pytest.raises(InvalidInput, match="does not take a thinking setting"):
        registry.check("o:deepseek-v4-flash", "high")
    registry.check("o:deepseek-v4-flash", "default")
