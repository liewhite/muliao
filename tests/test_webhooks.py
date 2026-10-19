"""webhook 触发器：token 鉴权、每个事件新建或续写会话、事件内容进入 prompt、不自动推送。"""

import httpx

from muliao.models import Origin, TaskKind
from tests.conftest import login
from tests.test_flow import drain, last_user_prompt, tasks_of


async def _create(client: httpx.AsyncClient, agent_id: str, name: str = "github") -> tuple[dict, str]:
    """建一个 webhook 触发器，并像用户在界面上那样生成 token。"""
    resp = await client.post(
        "/api/triggers",
        json={
            "name": name,
            "agent_id": agent_id,
            "kind": "webhook",
            "prompt": "Triage this GitHub event.",
            "config": {},
        },
    )
    assert resp.status_code == 201, resp.text
    trigger = resp.json()
    assert trigger["status"]["has_token"] is False
    token = (await client.post(f"/api/triggers/{trigger['id']}/token")).json()["token"]
    return trigger, token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_each_event_starts_a_new_session_with_payload_in_prompt(client, container, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    trigger, token = await _create(client, alpha)
    assert trigger["status"]["url"] == "http://test/api/hooks/github"

    first = await client.post("/api/hooks/github", content='{"action": "opened"}', headers=_bearer(token))
    second = await client.post("/api/hooks/github", content='{"action": "closed"}', headers=_bearer(token))
    assert first.status_code == second.status_code == 202
    await drain(container)

    sessions = await container.sessions.list(agent_id=alpha, origins=[Origin.TRIGGER], limit=10, offset=0)
    assert {s.id for s in sessions} == {first.json()["session_id"], second.json()["session_id"]}
    prompt = last_user_prompt(await container.sessions.history(first.json()["session_id"]))
    assert prompt == 'Triage this GitHub event.\n\n<event>\n{"action": "opened"}\n</event>'
    # 事件触发的一轮不自动推送
    assert await tasks_of(container, TaskKind.DELIVER) == []
    listed = (await client.get("/api/triggers")).json()
    assert listed[0]["last_session_id"] == second.json()["session_id"]


async def test_hook_rejects_wrong_token_and_unknown_trigger_alike(client, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    await _create(client, alpha)

    wrong = await client.post("/api/hooks/github", content="x", headers=_bearer("nope"))
    unknown = await client.post("/api/hooks/missing", content="x", headers=_bearer("nope"))
    missing = await client.post("/api/hooks/github", content="x")
    assert wrong.status_code == unknown.status_code == missing.status_code == 401
    assert wrong.json() == unknown.json()


async def test_hook_does_not_need_login_but_management_does(client, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    _, token = await _create(client, alpha)
    client.cookies.clear()

    assert (await client.get("/api/triggers")).status_code == 401
    assert (await client.post("/api/hooks/github", content="ping", headers=_bearer(token))).status_code == 202


async def test_rotated_token_replaces_old_one_and_disabled_trigger_refuses(client, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    trigger, old = await _create(client, alpha)

    new = (await client.post(f"/api/triggers/{trigger['id']}/token")).json()["token"]
    assert (await client.post("/api/hooks/github", content="x", headers=_bearer(old))).status_code == 401
    assert (await client.post("/api/hooks/github", content="x", headers=_bearer(new))).status_code == 202

    resp = await client.patch(f"/api/triggers/{trigger['id']}", json={"enabled": False})
    assert resp.status_code == 200
    assert (await client.post("/api/hooks/github", content="x", headers=_bearer(new))).status_code == 409


async def test_invalid_trigger_definitions_and_payloads_are_rejected(client, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    bad_name = await client.post(
        "/api/triggers",
        json={"name": "Bad Name", "agent_id": alpha, "kind": "webhook", "prompt": "p", "config": {}},
    )
    assert bad_name.status_code == 422
    _, token = await _create(client, alpha)
    assert (
        await client.post(
            "/api/triggers",
            json={"name": "github", "agent_id": alpha, "kind": "webhook", "prompt": "p", "config": {}},
        )
    ).status_code == 409

    assert (await client.post("/api/hooks/github", content="  ", headers=_bearer(token))).status_code == 422
    assert (
        await client.post("/api/hooks/github", content=b"\xff", headers=_bearer(token))
    ).status_code == 422


async def test_event_can_continue_the_session_it_started(client, container, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    _, token = await _create(client, alpha)

    first = (await client.post("/api/hooks/github", content="opened #1", headers=_bearer(token))).json()
    resp = await client.post(
        f"/api/hooks/github?session_id={first['session_id']}",
        content="commented on #1",
        headers=_bearer(token),
    )
    assert resp.status_code == 202 and resp.json() == first
    await drain(container)

    history = await container.sessions.history(first["session_id"])
    assert last_user_prompt(history).endswith("<event>\ncommented on #1\n</event>")
    assert len(await container.sessions.list(agent_id=alpha, origins=None, limit=10, offset=0)) == 1


async def test_event_cannot_write_into_sessions_it_did_not_start(client, container, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    _, github = await _create(client, alpha, "github")
    _, slack = await _create(client, alpha, "slack")
    user_session = (
        await client.post("/api/sessions", json={"agent_id": alpha, "prompt": "private chat"})
    ).json()
    slack_session = (await client.post("/api/hooks/slack", content="hi", headers=_bearer(slack))).json()

    for target in (user_session["id"], slack_session["session_id"]):
        resp = await client.post(
            f"/api/hooks/github?session_id={target}", content="x", headers=_bearer(github)
        )
        assert resp.status_code == 403, resp.text
        assert "github" not in resp.text and "slack" not in resp.text
    missing = await client.post("/api/hooks/github?session_id=nope", content="x", headers=_bearer(github))
    assert missing.status_code == 404


async def test_recreated_trigger_cannot_continue_sessions_of_the_deleted_one(client, make_agent):
    """触发器 id 是发送方身份：删后新建的触发器不能拿到旧 id，从而续写旧触发器的会话。"""
    alpha = (await make_agent("alpha")).id
    await login(client)
    old, old_token = await _create(client, alpha, "github")
    session = (await client.post("/api/hooks/github", content="x", headers=_bearer(old_token))).json()[
        "session_id"
    ]
    assert (await client.delete(f"/api/triggers/{old['id']}")).status_code == 204

    new, new_token = await _create(client, alpha, "github")
    assert new["id"] != old["id"]
    resp = await client.post(
        f"/api/hooks/github?session_id={session}", content="y", headers=_bearer(new_token)
    )
    assert resp.status_code == 403


async def test_oversized_payload_is_refused_and_unauthenticated_body_is_never_read(client, make_agent):
    from muliao.triggers.service import EVENT_MAX_BYTES

    alpha = (await make_agent("alpha")).id
    await login(client)
    _, token = await _create(client, alpha)
    too_big = b"x" * (EVENT_MAX_BYTES + 1)

    assert (
        await client.post("/api/hooks/github", content=too_big, headers=_bearer(token))
    ).status_code == 413

    async def chunks():
        # 不声明长度的分块上传：超限即停
        for _ in range(3):
            yield b"x" * (EVENT_MAX_BYTES // 2)

    assert (
        await client.post("/api/hooks/github", content=chunks(), headers=_bearer(token))
    ).status_code == 413

    consumed = {"chunks": 0}

    async def counting():
        consumed["chunks"] += 1
        yield b"x"

    resp = await client.post("/api/hooks/github", content=counting(), headers=_bearer("wrong"))
    assert resp.status_code == 401 and consumed["chunks"] == 0


async def test_webhook_without_token_rejects_events(client, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    resp = await client.post(
        "/api/triggers",
        json={"name": "fresh", "agent_id": alpha, "kind": "webhook", "prompt": "p", "config": {}},
    )
    assert resp.status_code == 201
    hook = await client.post("/api/hooks/fresh", content="x", headers=_bearer("anything"))
    assert hook.status_code == 401


async def test_schedule_trigger_cannot_be_fired_over_http(client, make_agent):
    alpha = (await make_agent("alpha")).id
    await login(client)
    resp = await client.post(
        "/api/triggers",
        json={
            "name": "daily",
            "agent_id": alpha,
            "kind": "schedule",
            "prompt": "p",
            "config": {"cron": "0 9 * * *"},
        },
    )
    trigger = resp.json()
    assert trigger["status"]["next_run_at"]
    assert (await client.post(f"/api/triggers/{trigger['id']}/token")).status_code == 422
