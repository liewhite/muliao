"""agent 的分组与归档（#219）：归档的 agent 不接受来信、不能被指派、触发器不触发；数据与配置照常可改，取消归档后恢复。"""

import pytest
from sqlalchemy import select

from muliao.agents.api import AgentArchived, AgentPatch, AgentSpec
from muliao.errors import Conflict, InvalidInput
from muliao.issues.api import IssueStatus
from muliao.sessions.api import Origin
from muliao.sessions.delegation_tools import AgentGroup, AgentInfo, agent_directory
from muliao.sessions.inbound import Inbound, NewSession
from muliao.sessions.tables import Session
from muliao.triggers.api import Precheck
from tests.conftest import login
from tests.test_flow import deliver, drain

TODO = IssueStatus.TODO


def _new_session(agent_id: str) -> Inbound:
    return Inbound(
        origin=Origin.USER,
        origin_ref=None,
        content="hi",
        session=NewSession(agent_id=agent_id, title="t"),
        awaited=False,
    )


async def test_group_is_trimmed_kept_on_other_changes_and_cleared_by_an_empty_string(container):
    agent = await container.agents.create(
        AgentSpec(name="a", description="d", model="test", group="  jobs  "), author="user"
    )
    assert agent.group == "jobs"
    assert (await container.agents.update(agent.id, AgentPatch(description="x"))).group == "jobs"
    assert (await container.agents.update(agent.id, AgentPatch(group="   "))).group is None
    with pytest.raises(InvalidInput, match="at most 64"):
        await container.agents.update(agent.id, AgentPatch(group="g" * 65))


async def test_list_agents_groups_ungrouped_first_and_leaves_out_archived(container, make_agent):
    plain = await make_agent("plain")
    for name, group in (("z1", "zhu"), ("b1", "biz"), ("z2", "zhu")):
        await container.agents.create(
            AgentSpec(name=name, description=name, model="test", group=group), author="user"
        )
    gone = await make_agent("gone")
    await container.agent_lifecycle.set_archived(gone.id, True)

    directory = agent_directory(await container.agents.list())
    assert directory == [
        AgentGroup(group=None, agents=[AgentInfo(name="plain", description=plain.description)]),
        AgentGroup(group="biz", agents=[AgentInfo(name="b1", description="b1")]),
        AgentGroup(
            group="zhu",
            agents=[AgentInfo(name="z1", description="z1"), AgentInfo(name="z2", description="z2")],
        ),
    ]


async def test_an_archived_agent_takes_no_mail_until_unarchived(container, make_agent):
    alpha = await make_agent("alpha")
    archived = await container.agent_lifecycle.set_archived(alpha.id, True)
    assert archived.archived
    # 已是目标状态：不做任何事
    assert (await container.agent_lifecycle.set_archived(alpha.id, True)).archived_at == archived.archived_at

    with pytest.raises(AgentArchived, match="archived; unarchive it first"):
        await container.sessions.receive(_new_session(alpha.id))
    # 配置照常可改（取消归档、改名都要能找到它）
    assert (await container.agents.update(alpha.id, AgentPatch(group="old"))).group == "old"
    assert (await container.agents.find_by_name("alpha")).archived

    assert not (await container.agent_lifecycle.set_archived(alpha.id, False)).archived
    received = await container.sessions.receive(_new_session(alpha.id))
    assert received.session.agent_id == alpha.id


async def test_existing_sessions_of_an_archived_agent_refuse_new_messages(container, make_agent):
    alpha = await make_agent("alpha")
    session = (await container.sessions.receive(_new_session(alpha.id))).session
    await drain(container)
    await container.agent_lifecycle.set_archived(alpha.id, True)
    follow_up = Inbound(
        origin=Origin.USER, origin_ref=None, content="more", session=session.id, awaited=False
    )
    with pytest.raises(AgentArchived):
        await container.sessions.receive(follow_up)


async def test_archiving_needs_the_agent_idle_and_not_default(container, make_agent):
    alpha = await make_agent("alpha")
    await container.sessions.receive(_new_session(alpha.id))
    # 信件在排队、还没运行：忙
    with pytest.raises(Conflict, match="working right now"):
        await container.agent_lifecycle.set_archived(alpha.id, True)
    await drain(container)

    await container.agents.set_default(alpha.id, True)
    with pytest.raises(Conflict, match="default agent"):
        await container.agent_lifecycle.set_archived(alpha.id, True)
    await container.agents.set_default(alpha.id, False)
    await container.agent_lifecycle.set_archived(alpha.id, True)
    # 归档的不能设为默认
    with pytest.raises(AgentArchived):
        await container.agents.set_default(alpha.id, True)


async def test_mail_validated_before_archiving_is_dropped_on_delivery(container, make_agent):
    alpha = await make_agent("alpha")
    trigger = await container.triggers.create(
        name="h", agent_id=alpha.id, kind="webhook", prompt="p", config={}
    )
    # 触发记下了、投信的定向命令还在排队（只校验了来信，会话还没建，agent 不忙）
    async with container.db.transaction() as s:
        fired = await container.triggers._deliver_in(  # noqa: SLF001
            s, await s.get(type(trigger), trigger.id), None, None
        )
    await container.agent_lifecycle.set_archived(alpha.id, True)
    await deliver(container)
    async with container.db.transaction() as s:
        assert await s.get(Session, fired.session_id) is None


async def test_triggers_of_an_archived_agent_neither_run_their_precheck_nor_fire(
    container, make_agent, tmp_path
):
    alpha = await make_agent("alpha")
    marker = tmp_path / "ran"
    trigger = await container.triggers.create(
        name="w",
        agent_id=alpha.id,
        kind="webhook",
        prompt="p",
        config={},
        precheck=Precheck(command=f"touch {marker}; echo event"),
    )
    await container.agent_lifecycle.set_archived(alpha.id, True)

    with pytest.raises(AgentArchived):
        await container.triggers.fire(trigger.id)
    # 激活机制：只跳过，不停用、不记失败
    await container.triggers.fire_activated(trigger.id)
    after = await container.triggers.get_by_name("w")
    assert after.enabled and after.precheck_error is None and after.disabled_reason is None
    assert not marker.exists()
    with pytest.raises(AgentArchived):
        await container.triggers.create(name="x", agent_id=alpha.id, kind="webhook", prompt="p", config={})

    await container.agent_lifecycle.set_archived(alpha.id, False)
    assert await container.triggers.fire(trigger.id) is not None
    assert marker.exists()


async def test_issues_cannot_go_to_an_archived_agent_and_its_old_ones_stay_quiet(container, make_agent):
    alpha, beta = await make_agent("alpha"), await make_agent("beta")
    issue = await container.issues.create(None, owner=alpha.id, title="t", description="", assignee=alpha.id)
    await container.agent_lifecycle.set_archived(beta.id, True)
    with pytest.raises(AgentArchived):
        await container.issues.update(None, issue.id, assignee=beta.id)
    with pytest.raises(AgentArchived):
        await container.issues.update(None, issue.id, owner=beta.id)
    with pytest.raises(AgentArchived):
        await container.issues.create(None, owner=beta.id, title="t2", description="")

    # 已指派给它的 issue：它归档后，用户的改动与评论照常记下，只是不投递
    await container.agent_lifecycle.set_archived(alpha.id, True)
    await container.issues.update(None, issue.id, status=TODO)
    await container.issues.comment(None, issue.id, "still there?")
    await deliver(container)
    async with container.db.transaction() as s:
        assert (await s.scalars(select(Session).where(Session.origin == Origin.ISSUE))).all() == []
    activities = (await container.issues.get(issue.id)).activities
    assert any(a.body == "still there?" for a in activities)


async def test_archive_over_http_marks_the_agent_and_conversations_get_409(client):
    await login(client)
    agent = (await client.post("/api/agents", json={"name": "a", "description": "x", "group": "jobs"})).json()
    assert agent["group"] == "jobs" and agent["archived_at"] is None
    path = f"/api/agents/{agent['id']}/archive"
    archived = (await client.put(path)).json()
    assert archived["archived_at"] is not None
    # 列表照常返回它（带标记，界面自己分）
    assert [a["archived_at"] for a in (await client.get("/api/agents")).json()] == [archived["archived_at"]]
    resp = await client.post("/api/sessions", json={"agent_id": agent["id"], "prompt": "hi"})
    assert resp.status_code == 409 and "archived" in resp.text
    assert (await client.delete(path)).json()["archived_at"] is None
    # 整体替换时 null 即不分组
    body = {
        k: agent[k]
        for k in ("name", "description", "model", "roles", "allow", "deny", "mcp_servers", "thinking")
    }
    assert (await client.put(f"/api/agents/{agent['id']}", json={**body, "group": None})).json()[
        "group"
    ] is None
