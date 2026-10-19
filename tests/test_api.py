from collections.abc import AsyncIterator

import httpx
import pytest

from muliao.api.app import create_app
from muliao.api.auth import Auth


@pytest.fixture
async def client(container, tmp_path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container, tmp_path / "no-web")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/auth/login", json={"password": "password1"})
    assert resp.status_code == 204


async def test_api_requires_login(client):
    assert (await client.get("/api/agents")).status_code == 401
    assert (await client.post("/api/auth/login", json={"password": "nope"})).status_code == 401
    await login(client)
    assert (await client.get("/api/agents")).status_code == 200


async def test_agent_session_lifecycle_over_http(client):
    await login(client)
    resp = await client.post("/api/agents", json={"name": "alpha", "description": "general helper"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["model"] == "fake:default"
    assert (await client.post("/api/agents", json={"name": "alpha", "description": "dup"})).status_code == 409
    assert (
        await client.post("/api/agents", json={"name": "Bad Name", "description": "x"})
    ).status_code == 422

    resp = await client.post("/api/sessions", json={"agent_name": "alpha", "prompt": "hello there"})
    assert resp.status_code == 201
    session = resp.json()
    assert session["title"] == "hello there"

    detail = (await client.get(f"/api/sessions/{session['id']}")).json()
    assert detail["items"] == []
    assert [t["prompt"] for t in detail["open_turns"]] == ["hello there"]
    assert (await client.get("/api/sessions/nope")).status_code == 404


async def test_skill_and_memory_endpoints(client):
    await login(client)
    await client.post("/api/agents", json={"name": "alpha", "description": "general helper"})

    resp = await client.put(
        "/api/skill", json={"owner": None, "name": "shared", "description": "for all", "body": "steps"}
    )
    assert resp.status_code == 200
    assert [s["name"] for s in (await client.get("/api/skills")).json()] == ["shared"]
    assert (await client.get("/api/skills", params={"owner": "alpha"})).json() == []

    resp = await client.put("/api/agents/alpha/memory", json={"content": "- fact"})
    assert resp.json()["content"] == "- fact"
    assert (await client.put("/api/agents/alpha/memory", json={"content": "x" * 10_000})).status_code == 422


async def test_vault_never_returns_values(client):
    await login(client)
    assert (
        await client.put("/api/vault/SLACK_TOKEN", json={"value": "xoxb-1", "description": "slack"})
    ).status_code == 204
    listed = (await client.get("/api/vault")).json()
    assert listed[0]["key"] == "SLACK_TOKEN" and "xoxb-1" not in str(listed)


async def test_webhook_endpoint_is_public_but_token_protected(client):
    await login(client)
    await client.post("/api/agents", json={"name": "alpha", "description": "general helper"})
    hook = (
        await client.post("/api/webhooks", json={"name": "gh", "agent_name": "alpha", "prompt": "Handle"})
    ).json()
    client.cookies.clear()

    assert (
        await client.post(f"/hooks/{hook['id']}", content="{}", headers={"X-Muliao-Token": "bad"})
    ).status_code == 403
    resp = await client.post(f"/hooks/{hook['id']}", content="{}", headers={"X-Muliao-Token": hook["token"]})
    assert resp.status_code == 202 and resp.json()["session_id"]


def test_auth_token_expires_and_rejects_tampering():
    auth = Auth("secret", "pw")
    token = auth.issue(now=1000)
    assert auth.verify(token, now=1001)
    assert not auth.verify(token, now=1000 + 91 * 24 * 3600)
    assert not auth.verify(token.replace("v1.1000", "v1.2000"), now=1001)


async def test_chinese_agent_name_roundtrip_over_http(client, container):
    from urllib.parse import quote

    await login(client)
    resp = await client.post("/api/agents", json={"name": "研究助理", "description": "查资料"})
    assert resp.status_code == 201, resp.text
    path = f"/api/agents/{quote('研究助理')}"
    assert (await client.get(path)).json()["name"] == "研究助理"
    assert (await client.get(f"{path}/memory")).status_code == 200
    assert container.agents.workspace("研究助理").is_dir()

    session = (await client.post("/api/sessions", json={"agent_name": "研究助理", "prompt": "hi"})).json()
    assert session["agent_name"] == "研究助理"
    assert (await client.delete(path)).status_code == 204


async def test_agent_defaults_endpoint_reflects_config(client, container):
    await login(client)
    defaults = (await client.get("/api/agent-defaults")).json()
    assert defaults == {
        "model": container.settings.llm.default_model,
        "reflect_every": container.settings.runtime.default_reflect_every,
    }
    assert container.settings.runtime.default_reflect_every == 5
