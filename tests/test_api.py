import pytest

from muliao.api.auth import Auth
from tests.conftest import login


async def test_api_requires_login(client):
    assert (await client.get("/api/agents")).status_code == 401
    assert (await client.post("/api/auth/login", json={"password": "nope"})).status_code == 401
    await login(client)
    assert (await client.get("/api/agents")).status_code == 200


async def test_agent_session_lifecycle_over_http(client):
    await login(client)
    resp = await client.post("/api/agents", json={"name": "alpha", "description": "general helper"})
    assert resp.status_code == 201, resp.text
    agent = resp.json()
    assert agent["model"] == "fake:default" and len(agent["id"]) == 12
    assert (await client.post("/api/agents", json={"name": "alpha", "description": "dup"})).status_code == 409
    assert (await client.post("/api/agents", json={"name": "  ", "description": "x"})).status_code == 422

    resp = await client.post("/api/sessions", json={"agent_id": agent["id"], "prompt": "hello there"})
    assert resp.status_code == 201
    session = resp.json()
    assert session["title"] == "hello there"
    assert session["agent_id"] == agent["id"]

    detail = (await client.get(f"/api/sessions/{session['id']}")).json()
    assert detail["items"] == []
    assert [(m["kind"], m["content"], m["state"]) for m in detail["mail"]] == [
        ("message", "hello there", "queued")
    ]
    assert detail["run"]["status"] == "pending"
    assert (await client.get("/api/sessions/nope")).status_code == 404


async def test_skill_and_memory_endpoints(client):
    await login(client)
    agent_id = (
        await client.post("/api/agents", json={"name": "alpha", "description": "general helper"})
    ).json()["id"]

    resp = await client.put(
        "/api/skill", json={"owner": None, "name": "shared", "description": "for all", "body": "steps"}
    )
    assert resp.status_code == 200
    assert [s["name"] for s in (await client.get("/api/skills")).json()] == ["shared"]
    assert (await client.get("/api/skills", params={"owner": agent_id})).json() == []

    resp = await client.put(f"/api/agents/{agent_id}/memory", json={"content": "- fact"})
    assert resp.json()["content"] == "- fact"
    assert (
        await client.put(f"/api/agents/{agent_id}/memory", json={"content": "x" * 10_000})
    ).status_code == 422


async def test_vault_never_returns_values(client):
    await login(client)
    assert (
        await client.put("/api/vault/SLACK_TOKEN", json={"value": "xoxb-1", "description": "slack"})
    ).status_code == 204
    listed = (await client.get("/api/vault")).json()
    assert listed[0]["key"] == "SLACK_TOKEN" and listed[0]["owner"] is None and "xoxb-1" not in str(listed)
    assert (await client.put("/api/vault/X?owner=nope", json={"value": "v"})).status_code == 404


def test_auth_token_expires_and_rejects_tampering():
    auth = Auth("secret", "pw")
    token = auth.issue(now=1000)
    assert auth.verify(token, now=1001)
    assert not auth.verify(token, now=1000 + 91 * 24 * 3600)
    assert not auth.verify(token.replace("v1.1000", "v1.2000"), now=1001)


async def test_agent_identified_by_id_and_renamable(client, container):
    await login(client)
    agent = (await client.post("/api/agents", json={"name": "研究助理", "description": "查资料"})).json()
    path = f"/api/agents/{agent['id']}"
    # 目录用 id，不受名称影响
    assert container.agents.workspace(agent["id"]).is_dir()

    renamed = {**agent, "name": "Research Assistant 📚"}
    resp = await client.put(
        path,
        json={
            k: renamed[k]
            for k in ("name", "description", "model", "instructions", "reflect_every", "tool_groups")
        },
    )
    assert resp.status_code == 200 and resp.json()["name"] == "Research Assistant 📚"
    assert (await client.get(path)).json()["id"] == agent["id"]

    session = (await client.post("/api/sessions", json={"agent_id": agent["id"], "prompt": "hi"})).json()
    assert session["agent_id"] == agent["id"]  # 会话跟随 id，改名不影响

    other = (await client.post("/api/agents", json={"name": "other", "description": "x"})).json()
    clash = {
        "name": "other",
        "description": "d",
        "model": agent["model"],
        "instructions": "",
        "reflect_every": 5,
        "tool_groups": agent["tool_groups"],
    }
    assert (await client.put(path, json=clash)).status_code == 409
    assert (await client.delete(f"/api/agents/{other['id']}")).status_code == 204


async def test_agent_defaults_endpoint_reflects_config(client, container):
    await login(client)
    defaults = (await client.get("/api/agent-defaults")).json()
    assert defaults == {
        "model": container.settings.llm.default_model,
        "reflect_every": container.settings.runtime.default_reflect_every,
        "groups": [
            {"name": g.name, "label": g.label, "full_access": g.full_access, "default": g.default}
            for g in container.agents.defaults.groups
        ],
        "default_tool_groups": container.agents.defaults.tool_groups,
    }
    assert container.settings.runtime.default_reflect_every == 30


async def test_marking_notifications_read_stops_at_the_one_the_user_saw(client):
    await login(client)
    for _ in range(2):
        await client.post("/api/push/test")
    first, _ = sorted((await client.get("/api/notifications")).json(), key=lambda n: n["id"])
    assert (await client.get("/api/notifications/unread")).json() == {"count": 2}

    assert (await client.post("/api/notifications/read", json={"through_id": first["id"]})).status_code == 204

    # 后到的那条用户还没看到，保持未读
    notes = (await client.get("/api/notifications")).json()
    assert [n["unread"] for n in notes] == [True, False]
    assert (await client.get("/api/notifications/unread")).json() == {"count": 1}


async def test_a_failed_resource_write_leaves_no_half_created_agent(client, container, monkeypatch):
    """资源仓库提交失败时，agent 不能只剩数据库行（之后它的每一轮与 agent 列表都会因缺指令文件失败）。"""
    await login(client)

    # 文件照常写下，只有 git 提交失败（如 index.lock 被占用）：apply 要把写下的东西连同建出来的目录一起撤掉
    real_git = container.resources._git  # noqa: SLF001

    async def failing_git(*args, check=True):  # noqa: ANN001, ANN202
        if args and args[0] == "commit":
            raise RuntimeError("git index.lock is held")
        return await real_git(*args, check=check)

    from muliao.agents.service import AgentSpec

    await container.resources.ensure()
    monkeypatch.setattr(container.resources, "_git", failing_git)
    with pytest.raises(RuntimeError, match="index.lock"):
        await container.agents.create(AgentSpec(name="half", description="d", instructions=""), author="user")
    monkeypatch.undo()

    # 资源仓库里也没留下这个从未存在的 agent 的目录（否则启动检查会把它报成已删除 agent 的残留）
    assert container.resources.agent_owners() == []
    assert (await client.get("/api/agents")).status_code == 200
    assert (await client.post("/api/agents", json={"name": "half", "description": "d"})).status_code == 201


async def test_list_limits_must_be_positive(client):
    await login(client)
    assert (await client.get("/api/sessions", params={"limit": -1})).status_code == 422
    assert (await client.get("/api/sessions", params={"offset": -1})).status_code == 422
