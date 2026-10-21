"""模型选择：provider 来自配置，模型名向 provider 实时查询；查询失败原样报出。"""

from dataclasses import replace
from typing import get_args

import httpx2
import pytest
from pydantic import SecretStr
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from muliao.agent_runtime import llm
from muliao.config import LlmConfig, ProviderConfig, ProviderType
from muliao.errors import UpstreamFailed
from tests.conftest import login


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_openai_models_are_read_from_the_models_endpoint():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        data = [{"id": i, "object": "model", "created": 0, "owned_by": "x"} for i in ("chat", "reasoner")]
        return httpx2.Response(200, json={"object": "list", "data": data})

    provider = OpenAIProvider(api_key="k", base_url="https://api.example/v1", http_client=_client(handler))
    assert await llm.KINDS["openai-responses"].model_names(provider) == ["chat", "reasoner"]


async def test_anthropic_models_follow_every_page():
    pages = {
        None: {"data": [{"id": "a"}], "has_more": True, "first_id": "a", "last_id": "a"},
        "a": {"data": [{"id": "b"}], "has_more": False, "first_id": "b", "last_id": "b"},
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        page = pages[request.url.params.get("after_id")]
        data = [
            {**m, "type": "model", "display_name": m["id"], "created_at": "2026-01-01T00:00:00Z"}
            for m in page["data"]
        ]
        return httpx2.Response(200, json={**page, "data": data})

    provider = AnthropicProvider(api_key="k", base_url="https://api.example", http_client=_client(handler))
    assert await llm.KINDS["anthropic"].model_names(provider) == ["a", "b"]


async def test_google_keeps_chat_models_without_prefix_across_pages():
    pages = {
        None: {
            "models": [
                {
                    "name": "models/gemini-pro",
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {"name": "models/text-embedding", "supportedGenerationMethods": ["embedContent"]},
            ],
            "nextPageToken": "p2",
        },
        "p2": {
            "models": [{"name": "models/gemini-flash", "supportedGenerationMethods": ["generateContent"]}]
        },
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith("/models")
        return httpx2.Response(200, json=pages[request.url.params.get("pageToken")])

    provider = GoogleProvider(api_key="k", base_url="https://api.example/", http_client=_client(handler))
    assert await llm.KINDS["google"].model_names(provider) == ["gemini-pro", "gemini-flash"]


async def test_google_model_without_supported_actions_is_a_contract_violation():
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"models": [{"name": "models/gemini-pro"}]})

    provider = GoogleProvider(api_key="k", base_url="https://api.example/", http_client=_client(handler))
    with pytest.raises(RuntimeError, match="supported actions"):
        await llm.KINDS["google"].model_names(provider)


async def test_real_sdk_error_becomes_upstream_failure_with_reason():
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
        )

    config = LlmConfig(
        default_model="a:m", providers={"a": ProviderConfig(type="anthropic", api_key=SecretStr("k"))}
    )
    registry = llm.ModelRegistry(config)
    # 换掉连向真实地址的客户端：走完整的 SDK 报错路径，而不是直接伪造异常
    registry._providers["a"] = AnthropicProvider(
        api_key="k", base_url="https://api.example", http_client=_client(handler)
    )
    with pytest.raises(UpstreamFailed, match="bad key"):
        await registry.list_models("a")


def _stub_model_names(monkeypatch, fetch) -> None:
    """测试配置里唯一的 provider「fake」是 anthropic 类型：换掉这一类型的模型名查询。"""
    monkeypatch.setitem(llm.KINDS, "anthropic", replace(llm.KINDS["anthropic"], model_names=fetch))


def test_every_provider_type_has_a_kind():
    assert set(llm.KINDS) == set(get_args(ProviderType))


async def test_api_lists_configured_providers_and_their_models(client, monkeypatch):
    async def fetch(_provider):
        return ["z-model", "a-model"]

    _stub_model_names(monkeypatch, fetch)
    await login(client)

    assert (await client.get("/api/providers")).json() == ["fake"]
    assert (await client.get("/api/providers/fake/models")).json() == ["a-model", "z-model"]
    assert (await client.get("/api/providers/missing/models")).status_code == 404


async def test_provider_failure_is_reported_as_bad_gateway_with_reason(client, monkeypatch):
    async def fetch(_provider):
        raise httpx2.ConnectError("connection refused")

    _stub_model_names(monkeypatch, fetch)
    await login(client)

    resp = await client.get("/api/providers/fake/models")
    assert resp.status_code == 502
    assert "connection refused" in resp.json()["detail"] and "fake" in resp.json()["detail"]
