import json
from datetime import timedelta

from pydantic_ai.messages import ModelResponse
from pydantic_ai.profiles import ModelProfile

from muliao.agents.api import BASE
from muliao.api.auth import Auth
from tests.conftest import login, share
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
    assert agent["config"]["model"] == "fake:default" and len(agent["id"]) == 12
    assert (await client.post("/api/agents", json={"name": "alpha", "description": "dup"})).status_code == 409
    assert (await client.post("/api/agents", json={"name": "  ", "description": "x"})).status_code == 422
    assert agent["config"]["thinking"] == "default"  # 未指定时不传 thinking，与之前一致

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
        "/api/assets/skill/shared",
        json={"owner": None, "description": "for all", "body": "steps", "version": None},
    )
    assert resp.status_code == 200
    skills = [i for i in (await client.get("/api/assets")).json() if i["kind"] == "skill"]
    assert [(s["name"], s["owner"], s["description"], s["roles"]) for s in skills] == [
        ("shared", None, "for all", [])
    ]
    assert (await client.get("/api/assets/skill/shared")).json()["body"].strip() == "steps"

    version = (await client.get("/api/assets/memory/alpha")).json()["version"]
    resp = await client.put("/api/assets/memory/alpha", json={"content": "- fact", "version": version})
    assert (resp.json()["content"], resp.json()["owner"]) == ("- fact", agent_id)
    for name in ("alpha", "global"):
        version = (await client.get(f"/api/assets/memory/{name}")).json()["version"]
        too_long = {"content": "x" * 10_000, "version": version}
        assert (await client.put(f"/api/assets/memory/{name}", json=too_long)).status_code == 422


async def test_the_old_per_kind_endpoints_are_gone(client):
    await login(client)
    for path in (
        "/api/tools",
        "/api/mcp",
        "/api/skill?name=x",
        "/api/resources/memory/history",
        "/api/memory",
    ):
        assert (await client.get(path)).status_code == 404, path
    assert (await client.put("/api/vault/K", json={"value": "v"})).status_code == 404


async def test_a_skill_exported_over_http_imports_into_another_owner(client, container, make_agent):
    agent_id = (await make_agent()).id
    await login(client)
    await client.put(
        "/api/assets/skill/shared",
        json={"owner": None, "description": "for all", "body": "steps", "version": None},
    )
    # 二进制文件不进包，同导入一样列出来（响应头里的 JSON，非 ASCII 转义）
    (await container.skills.directory("shared") / "图.png").write_bytes(b"\x89PNG\xff")

    resp = await client.get("/api/assets/skill/shared/package")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == 'attachment; filename="shared.skill"'
    assert json.loads(resp.headers["x-skipped-files"]) == [{"path": "图.png", "reason": "not UTF-8 text"}]
    package = resp.content

    # 名字全局唯一：平台的还在时导入不进别的 owner
    assert (
        await client.post("/api/assets/skill", params={"owner": agent_id}, content=package)
    ).status_code == 409
    assert (await client.delete("/api/assets/skill/shared")).status_code == 204
    resp = await client.post("/api/assets/skill", params={"owner": agent_id}, content=package)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "skill": {"name": "shared", "description": "for all", "owner": agent_id},
        "skipped": [],
    }
    assert (await client.get("/api/assets/skill/shared/package")).status_code == 200
    assert (await client.get("/api/assets/skill/shared")).json()["body"].strip() == "steps"

    assert (
        await client.post("/api/assets/skill", params={"owner": agent_id}, content=package)
    ).status_code == 409
    replaced = await client.post(
        "/api/assets/skill", params={"owner": agent_id, "replace": True}, content=package
    )
    assert replaced.status_code == 200
    assert (await client.post("/api/assets/skill", content=b"not a zip")).status_code == 422
    assert (await client.get("/api/assets/skill/nope/package")).status_code == 404


async def test_assets_list_every_owners_assets_with_the_roles_that_give_them(client, container, make_agent):
    alpha = (await make_agent()).id
    await login(client)
    assert (
        await client.put("/api/assets/secret/SLACK_TOKEN", json={"value": "xoxb-1", "description": "slack"})
    ).status_code == 204
    await client.put("/api/assets/secret/MINE", json={"owner": alpha, "value": "v"})
    # key 全局唯一：同名就是改这一个，owner 不变
    assert (
        await client.put("/api/assets/secret/SLACK_TOKEN", json={"owner": alpha, "value": "v2"})
    ).status_code == 204
    assert (await client.put("/api/assets/secret/X", json={"owner": "nope", "value": "v"})).status_code == 404
    assert (await client.put("/api/assets/secret/bad-key", json={"value": "v"})).status_code == 422
    await share(container, "use:secret:SLACK_TOKEN")
    secret = (await client.get("/api/assets/secret/SLACK_TOKEN")).json()
    # 改值时没给说明：说明换成空的
    assert (secret["owner"], secret["description"]) == (None, "") and "value" not in secret

    assets = (await client.get("/api/assets")).json()
    secrets = [(i["name"], i["owner"], i["roles"]) for i in assets if i["kind"] == "secret"]
    assert secrets == [("MINE", alpha, []), ("SLACK_TOKEN", None, [BASE])]
    assert "xoxb-1" not in str(assets) and "v2" not in str(assets)
    tools = {i["name"]: i for i in assets if i["kind"] == "tool"}
    assert tools["run_shell"]["roles"] == [BASE, "admin"] and tools["delete_agent"]["roles"] == ["admin"]
    assert tools["run_shell"]["owner"] is None and tools["run_shell"]["updated_at"] is None
    # 内置工具在前
    assert assets[0]["kind"] == "tool"
    shared = {i["name"]: i["shared"] for i in assets if i["kind"] == "secret"}
    assert shared == {"MINE": False, "SLACK_TOKEN": True}


async def test_an_asset_given_by_another_agents_allow_is_shared_without_a_role(client, container, make_agent):
    from muliao.agents.service import AgentPatch

    alpha = (await make_agent()).id
    beta = (await make_agent("beta")).id
    await login(client)
    await client.put("/api/assets/secret/MINE", json={"owner": alpha, "value": "v"})
    await client.put("/api/assets/secret/LONELY", json={"value": "v"})
    await container.agents.update(beta, AgentPatch(allow=("use:secret:MINE",)), actor="user")
    secrets = {
        i["name"]: (i["roles"], i["shared"])
        for i in (await client.get("/api/assets")).json()
        if i["kind"] == "secret"
    }
    # 没有角色收录，但 beta 自己的 allow 放行它：分发出去了（删 alpha 时会转给平台，同一个定义）
    assert secrets == {"MINE": ([], True), "LONELY": ([], False)}


async def test_mcp_assets_carry_their_config_and_have_no_history(client):
    await login(client)
    body = {"owner": None, "transport": "http", "url": "https://example.com/mcp", "probe": False}
    assert (await client.put("/api/assets/mcp/remote", json=body)).status_code == 201
    assert (await client.put("/api/assets/mcp/remote", json=body)).status_code == 409
    assert (await client.put("/api/assets/mcp/remote/enabled", json={"enabled": False})).json()[
        "enabled"
    ] is False
    [item] = [i for i in (await client.get("/api/assets")).json() if i["kind"] == "mcp"]
    assert (item["name"], item["url"], item["transport"], item["enabled"]) == (
        "remote",
        "https://example.com/mcp",
        "http",
        False,
    )
    assert item["path"].endswith("/platform/mcp/remote.json")
    assert (await client.get("/api/assets/mcp/remote")).json()["enabled"] is False
    # 不再有版本历史与恢复（#498）
    assert (await client.get("/api/assets/mcp/remote/history")).status_code in (404, 405)
    assert (await client.delete("/api/assets/mcp/remote")).status_code == 204


def test_auth_token_expires_and_rejects_tampering():
    auth = Auth("secret", "pw")
    token = auth.issue(now=1000)
    assert auth.verify(token, now=1001)
    assert not auth.verify(token, now=1000 + 91 * 24 * 3600)
    assert not auth.verify(token.replace("v1.1000", "v1.2000"), now=1001)
    # 签名段带非 ASCII 字符只是对不上，不抛异常（否则请求变成 500）
    assert not auth.verify("v1.1000.签名", now=1001)


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
    resp = await client.patch(f"/api/agents/{agent['id']}", json={"name": " user "})
    assert resp.status_code == 422
    assert "reserved" in resp.json()["detail"]


async def test_agent_identified_by_id_and_renamable(client, container):
    await login(client)
    agent = (await client.post("/api/agents", json={"name": "研究助理", "description": "查资料"})).json()
    path = f"/api/agents/{agent['id']}"
    # 目录用 id，不受名称影响
    assert container.agents.workspace(agent["id"]).is_dir()

    resp = await client.patch(path, json={"name": "Research Assistant 📚"})
    assert resp.status_code == 200 and resp.json()["name"] == "Research Assistant 📚"
    assert (await client.get(path)).json()["id"] == agent["id"]

    session = (await client.post("/api/sessions", json={"agent_id": agent["id"], "prompt": "hi"})).json()
    assert session["agent_id"] == agent["id"]  # 会话跟随 id，改名不影响

    other = (await client.post("/api/agents", json={"name": "other", "description": "x"})).json()
    assert (await client.patch(path, json={"name": "other"})).status_code == 409
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
    assert defaults == {
        "name": "",
        "description": "",
        "model": container.settings.llm.default_model,
        "roles": [],
        "allow": [],
        "deny": [],
        "thinking": "default",
        "group": None,
    }


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
    """置顶与标记是同一条规则（工作状态）：在等到点唤醒的会话（没有运行）也置顶，且看的是全部会话，不只这一页。"""
    from muliao.platform.db import utcnow
    from muliao.sessions.wakeups import schedule_in
    from tests.test_flow import drain, start

    agent = await make_agent("alpha")
    oldest = await start(container, agent.id, "oldest")
    await drain(container)
    for prompt in ("middle", "newest"):
        await start(container, agent.id, prompt)
        await drain(container)
    async with container.db.transaction() as s:
        await schedule_in(s, oldest.id, "w1", utcnow() + timedelta(hours=1), "later", awaited=False)
    await login(client)

    [first] = (await client.get("/api/sessions", params={"limit": 1})).json()
    assert (first["id"], first["state"]) == (oldest.id, "waiting")
    detail = (await client.get(f"/api/sessions/{oldest.id}")).json()
    assert (detail["run"], detail["state"]) == (None, "waiting")

    # 没有运行也停得了：停止清掉占用，详情与列表都回到 idle
    assert (await client.post(f"/api/sessions/{oldest.id}/stop")).status_code == 202
    assert (await client.get(f"/api/sessions/{oldest.id}")).json()["state"] == "idle"
    assert [s["state"] for s in (await client.get("/api/sessions")).json()] == ["idle"] * 3


async def test_session_list_filters_by_keyword_in_title_or_messages(client, container, make_agent):
    """关键字匹配标题或消息原文（不区分 ASCII 大小写），可与来源筛选组合；% _ 按字面匹配，引号、换行按原文能搜到。"""
    from muliao.sessions.api import Origin
    from tests.test_flow import drain, start

    agent = await make_agent("alpha")
    by_title = await start(container, agent.id, "Deploy plan")
    # 标题只取前 20 个字，关键字在后面：只能从消息里搜到
    in_body = await start(container, agent.id, '01234567890123456789 说 "灰度 发布"\n100%_done')
    auto = await start(container, agent.id, "deploy by trigger", origin=Origin.TRIGGER)
    await start(container, agent.id, "unrelated")
    await drain(container)
    await login(client)

    async def ids(**params) -> set[str]:
        response = await client.get("/api/sessions", params=params)
        assert response.status_code == 200, response.text
        return {s["id"] for s in response.json()}

    assert await ids(q="DEPLOY") == {by_title.id, auto.id}
    assert await ids(q="deploy", origin="user") == {by_title.id}
    assert await ids(q='"灰度 发布"\n100') == {in_body.id}
    assert await ids(q="0%_d") == {in_body.id}
    assert await ids(q="%") == {in_body.id}
    assert await ids(q="nothing-like-this") == set()
    # 只匹配消息的文字，不匹配消息 JSON 的键名与结构
    assert await ids(q="part_kind") == set()
    assert (await client.get("/api/sessions", params={"q": ""})).status_code == 422


async def test_an_agent_being_deleted_reads_as_missing(client, container, make_agent):
    """删除窗口里读它一律 404，不读到一半变成 500（它的资源目录在删除流程收尾时才删）。"""
    from muliao.agents.tables import Agent

    agent = await make_agent("alpha")
    await login(client)
    async with container.db.transaction() as s:
        (await s.get(Agent, agent.id)).deleting_by = "user"

    assert (await client.get(f"/api/agents/{agent.id}")).status_code == 404
    assert (await client.get(f"/api/agents/{agent.id}/history")).status_code == 404
    assert (await client.get(f"/api/agents/{agent.id}/assets")).status_code == 404


async def test_agent_thinking_is_set_and_validated_over_http(client, models):
    await login(client)
    models.models["fake:default"] = scripted(
        lambda _m, _i: ModelResponse(parts=[]), ModelProfile(supports_thinking=True)
    )
    resp = await client.post("/api/agents", json={"name": "alpha", "description": "d", "thinking": "high"})
    assert resp.status_code == 201, resp.text
    agent = resp.json()
    assert agent["config"]["thinking"] == "high"
    path = f"/api/agents/{agent['id']}"
    resp = await client.patch(path, json={"thinking": "off"})
    assert resp.json()["config"]["thinking"] == "off"
    # 没给的字段不变；不在档位里的值拒绝
    assert (await client.patch(path, json={"description": "d2"})).json()["config"]["thinking"] == "off"
    resp = await client.patch(path, json={"thinking": "max"})
    assert resp.status_code == 422
    assert (await client.get(f"/api/agents/{agent['id']}")).json()["config"]["thinking"] == "off"
