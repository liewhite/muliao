from pydantic_ai.messages import ModelResponse
from pydantic_ai.profiles import ModelProfile

from muliao.api.auth import Auth
from tests.conftest import login
from tests.scripted import scripted


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
    assert agent["thinking"] == "default"  # 未指定时不传 thinking，与之前一致

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

    resp = await client.put("/api/memory", json={"owner": agent_id, "content": "- fact"})
    assert resp.json()["content"] == "- fact"
    for owner in (agent_id, None):
        too_long = {"owner": owner, "content": "x" * 10_000}
        assert (await client.put("/api/memory", json=too_long)).status_code == 422


async def test_a_skill_exported_over_http_imports_into_another_owner(client, make_agent):
    agent_id = (await make_agent()).id
    await login(client)
    await client.put(
        "/api/skill", json={"owner": None, "name": "shared", "description": "for all", "body": "steps"}
    )

    resp = await client.get("/api/skill/package", params={"name": "shared"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == 'attachment; filename="shared.skill"'
    package = resp.content

    # 名字全局唯一：公共的还在时导入不进别的 owner
    assert (
        await client.post("/api/skill/package", params={"owner": agent_id}, content=package)
    ).status_code == 409
    assert (await client.delete("/api/skill", params={"name": "shared"})).status_code == 204
    resp = await client.post("/api/skill/package", params={"owner": agent_id}, content=package)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "skill": {"name": "shared", "description": "for all", "owner": agent_id},
        "skipped": [],
    }
    exported = await client.get("/api/skill/package", params={"name": "shared", "owner": agent_id})
    assert exported.status_code == 200
    assert (await client.get("/api/skill", params={"name": "shared", "owner": agent_id})).json()[
        "body"
    ].strip() == "steps"

    assert (
        await client.post("/api/skill/package", params={"owner": agent_id}, content=package)
    ).status_code == 409
    replaced = await client.post(
        "/api/skill/package", params={"owner": agent_id, "replace": True}, content=package
    )
    assert replaced.status_code == 200
    assert (await client.post("/api/skill/package", content=b"not a zip")).status_code == 422
    assert (await client.get("/api/skill/package", params={"name": "nope"})).status_code == 404


async def test_vault_never_returns_values(client):
    await login(client)
    assert (
        await client.put("/api/vault/SLACK_TOKEN", json={"value": "xoxb-1", "description": "slack"})
    ).status_code == 204
    listed = (await client.get("/api/vault")).json()
    assert listed[0]["key"] == "SLACK_TOKEN" and listed[0]["owner"] is None and "xoxb-1" not in str(listed)
    assert (await client.put("/api/vault/X?owner=nope", json={"value": "v"})).status_code == 404


async def test_vault_and_mcp_lists_take_the_owners_view(client, make_agent):
    alpha = (await make_agent()).id
    await login(client)
    await client.put(f"/api/vault/MINE?owner={alpha}", json={"value": "v"})
    await client.put("/api/vault/OTHER", json={"value": "v"})
    # key 全局唯一
    assert (await client.put(f"/api/vault/OTHER?owner={alpha}", json={"value": "v"})).status_code == 409

    def keys(listed):
        return [(s["key"], s["owner"]) for s in listed]

    assert keys((await client.get("/api/vault", params={"owner": alpha})).json()) == [
        ("MINE", alpha),
        ("OTHER", None),
    ]
    assert keys((await client.get("/api/vault")).json()) == [("OTHER", None)]
    assert (await client.get("/api/vault", params={"owner": "nope"})).status_code == 404
    assert (await client.get("/api/mcp", params={"owner": "nope"})).status_code == 404


def test_auth_token_expires_and_rejects_tampering():
    auth = Auth("secret", "pw")
    token = auth.issue(now=1000)
    assert auth.verify(token, now=1001)
    assert not auth.verify(token, now=1000 + 91 * 24 * 3600)
    assert not auth.verify(token.replace("v1.1000", "v1.2000"), now=1001)


def test_changing_the_password_revokes_issued_cookies():
    token = Auth("secret", "old").issue(now=1000)
    assert Auth("secret", "old").verify(token, now=1001)
    assert not Auth("secret", "new").verify(token, now=1001)
    assert not Auth("other", "old").verify(token, now=1001)


async def test_agent_cannot_be_named_user(client):
    # 模型按名称指派 issue 时 `user` 指用户：同名的 agent 会分不出来
    await login(client)
    assert (await client.post("/api/agents", json={"name": "user", "description": "x"})).status_code == 422
    agent = (await client.post("/api/agents", json={"name": "a1", "description": "x"})).json()
    resp = await client.put(f"/api/agents/{agent['id']}", json={**agent, "name": " user "})
    assert resp.status_code == 422
    assert "reserved" in resp.json()["detail"]


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
            for k in (
                "name",
                "description",
                "model",
                "roles",
                "allow",
                "deny",
                "mcp_servers",
                "thinking",
                "group",
            )
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
        "roles": agent["roles"],
        "allow": [],
        "deny": [],
        "mcp_servers": [],
        "thinking": "default",
        "group": None,
    }
    assert (await client.put(path, json=clash)).status_code == 409
    assert (await client.delete(f"/api/agents/{other['id']}")).status_code == 204


async def test_default_agent_is_at_most_one_and_goes_with_the_agent(client):
    await login(client)
    a = (await client.post("/api/agents", json={"name": "a", "description": "x"})).json()
    b = (await client.post("/api/agents", json={"name": "b", "description": "x"})).json()
    assert not a["is_default"]

    async def defaults() -> list[str]:
        return [x["name"] for x in (await client.get("/api/agents")).json() if x["is_default"]]

    resp = await client.put(f"/api/agents/{a['id']}/default")
    assert resp.status_code == 200 and resp.json()["is_default"]
    assert await defaults() == ["a"]
    # 再设一次仍是默认
    assert (await client.put(f"/api/agents/{a['id']}/default")).json()["is_default"]
    assert await defaults() == ["a"]
    # 设另一个为默认就取代原来的
    assert (await client.put(f"/api/agents/{b['id']}/default")).json()["is_default"]
    assert await defaults() == ["b"]
    # 取消不是默认的 agent 不影响现有默认
    assert not (await client.delete(f"/api/agents/{a['id']}/default")).json()["is_default"]
    assert await defaults() == ["b"]
    assert (await client.put("/api/agents/nope/default")).status_code == 404
    # 删除默认 agent 后没有默认
    assert (await client.delete(f"/api/agents/{b['id']}")).status_code == 204
    assert await defaults() == []
    assert (await client.put(f"/api/agents/{a['id']}/default")).json()["is_default"]
    assert not (await client.delete(f"/api/agents/{a['id']}/default")).json()["is_default"]
    assert await defaults() == []


async def test_agent_defaults_endpoint_matches_config(client, container):
    await login(client)
    defaults = (await client.get("/api/agent-defaults")).json()
    assert defaults == {"model": container.settings.llm.default_model, "roles": ["agent"]}


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


async def test_list_limits_must_be_positive(client):
    await login(client)
    assert (await client.get("/api/sessions", params={"limit": -1})).status_code == 422
    assert (await client.get("/api/sessions", params={"offset": -1})).status_code == 422
    assert (await client.get("/api/tasks", params={"limit": -1})).status_code == 422
    assert (await client.get("/api/tasks", params={"limit": 0})).status_code == 422


async def test_opening_a_session_marks_it_and_its_notifications_read(client, container, make_agent):
    from tests.test_flow import drain, start

    agent = await make_agent("alpha")
    session = await start(container, agent.id, "hi")
    await drain(container)
    await login(client)
    assert (await client.get("/api/notifications/unread")).json() == {"count": 1}
    assert (await client.get(f"/api/sessions/{session.id}")).json()["session"]["unread"]
    assert [s["unread"] for s in (await client.get("/api/sessions")).json()] == [True]

    assert (await client.post(f"/api/sessions/{session.id}/read")).status_code == 204

    assert (await client.get("/api/notifications/unread")).json() == {"count": 0}
    assert not (await client.get(f"/api/sessions/{session.id}")).json()["session"]["unread"]
    assert [s["unread"] for s in (await client.get("/api/sessions")).json()] == [False]


async def test_session_list_pins_running_sessions_and_marks_them(client, container, make_agent):
    from muliao.platform.tables import Task, TaskStatus
    from muliao.sessions.lanes import RUN_TASK
    from tests.test_flow import drain, start

    agent = await make_agent("alpha")
    older = await start(container, agent.id, "older")
    await drain(container)
    newer = await start(container, agent.id, "newer")
    await drain(container)
    await login(client)
    listed = (await client.get("/api/sessions")).json()
    assert [(s["id"], s["state"]) for s in listed] == [(newer.id, "idle"), (older.id, "idle")]

    # 较早的会话又在运行（运行任务进行中）：置顶；排队的运行同样算在运行（工作状态），置顶的之间按最近更新排序
    async with container.db.transaction() as s:
        s.add(
            Task(
                kind=RUN_TASK,
                lane=f"session:{older.id}",
                agent_id=agent.id,
                session_id=older.id,
                payload={},
                status=TaskStatus.RUNNING,
            )
        )
        s.add(
            Task(
                kind=RUN_TASK,
                lane=f"session:{newer.id}",
                agent_id=agent.id,
                session_id=newer.id,
                payload={},
                status=TaskStatus.PENDING,
            )
        )
    listed = (await client.get("/api/sessions")).json()
    assert [(s["id"], s["state"]) for s in listed] == [(newer.id, "running"), (older.id, "running")]
    assert (await client.get("/api/sessions", params={"origin": "trigger"})).json() == []


async def test_session_list_pins_every_session_that_is_not_idle_even_beyond_the_first_page(
    client, container, make_agent
):
    """置顶与标记是同一条规则（工作状态）：在等后台任务的会话（没有运行）也置顶，且看的是全部会话，不只这一页。"""
    from muliao.sessions.holds import hold_in
    from tests.test_flow import drain, start

    agent = await make_agent("alpha")
    oldest = await start(container, agent.id, "oldest")
    await drain(container)
    for prompt in ("middle", "newest"):
        await start(container, agent.id, prompt)
        await drain(container)
    async with container.db.transaction() as s:
        await hold_in(s, oldest.id, "job:1", awaited=False)
    await login(client)

    [first] = (await client.get("/api/sessions", params={"limit": 1})).json()
    assert (first["id"], first["state"]) == (oldest.id, "waiting")
    detail = (await client.get(f"/api/sessions/{oldest.id}")).json()
    assert (detail["run"], detail["state"]) == (None, "waiting")

    # 没有运行也停得了：停止清掉占用，详情与列表都回到 idle
    assert (await client.post(f"/api/sessions/{oldest.id}/stop")).status_code == 202
    assert (await client.get(f"/api/sessions/{oldest.id}")).json()["state"] == "idle"
    assert [s["state"] for s in (await client.get("/api/sessions")).json()] == ["idle"] * 3


async def test_an_agent_being_deleted_reads_as_missing(client, container, make_agent):
    """删除窗口里它的私有资源（指令、记忆）可能已经删了：读它与它的资源一律 404，不读到一半变成 500。"""
    from muliao.agents.tables import Agent

    agent = await make_agent("alpha")
    async with container.db.transaction() as s:
        (await s.get(Agent, agent.id)).deleting_by = "user"
    await login(client)

    assert (await client.get(f"/api/agents/{agent.id}")).status_code == 404
    assert (await client.get("/api/memory", params={"owner": agent.id})).status_code == 404


async def test_agent_thinking_is_set_and_validated_over_http(client, models):
    await login(client)
    models.models["fake:default"] = scripted(
        lambda _m, _i: ModelResponse(parts=[]), ModelProfile(supports_thinking=True)
    )
    resp = await client.post("/api/agents", json={"name": "alpha", "description": "d", "thinking": "high"})
    assert resp.status_code == 201, resp.text
    agent = resp.json()
    assert agent["thinking"] == "high"
    body = {
        k: agent[k]
        for k in ("name", "description", "model", "roles", "allow", "deny", "mcp_servers", "group")
    }
    resp = await client.put(f"/api/agents/{agent['id']}", json={**body, "thinking": "off"})
    assert resp.json()["thinking"] == "off"
    # 整体替换：thinking 必填；不在档位里的值拒绝
    assert (await client.put(f"/api/agents/{agent['id']}", json=body)).status_code == 422
    resp = await client.put(f"/api/agents/{agent['id']}", json={**body, "thinking": "max"})
    assert resp.status_code == 422
    assert (await client.get(f"/api/agents/{agent['id']}")).json()["thinking"] == "off"
