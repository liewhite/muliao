"""模型选择：provider 来自配置，模型名向 provider 实时查询；查询失败原样报出。"""

import httpx2
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from muliao import llm
from tests.conftest import login


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_openai_models_are_read_from_the_models_endpoint():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        data = [{"id": i, "object": "model", "created": 0, "owned_by": "x"} for i in ("chat", "reasoner")]
        return httpx2.Response(200, json={"object": "list", "data": data})

    provider = OpenAIProvider(api_key="k", base_url="https://api.example/v1", http_client=_client(handler))
    assert await llm._fetch_model_names("openai-responses", provider) == ["chat", "reasoner"]


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
    assert await llm._fetch_model_names("anthropic", provider) == ["a", "b"]


async def test_api_lists_configured_providers_and_their_models(client, monkeypatch):
    async def fetch(kind, _provider):
        assert kind == "anthropic"
        return ["z-model", "a-model"]

    monkeypatch.setattr(llm, "_fetch_model_names", fetch)
    await login(client)

    assert (await client.get("/api/providers")).json() == ["fake"]
    assert (await client.get("/api/providers/fake/models")).json() == ["a-model", "z-model"]
    assert (await client.get("/api/providers/missing/models")).status_code == 404


async def test_provider_failure_is_reported_as_bad_gateway_with_reason(client, monkeypatch):
    async def fetch(_kind, _provider):
        raise httpx2.ConnectError("connection refused")

    monkeypatch.setattr(llm, "_fetch_model_names", fetch)
    await login(client)

    resp = await client.get("/api/providers/fake/models")
    assert resp.status_code == 502
    assert "connection refused" in resp.json()["detail"] and "fake" in resp.json()["detail"]
