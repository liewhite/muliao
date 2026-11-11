"""按字段乐观锁（#634，api.fieldlock）：PATCH 带上改动字段开始编辑时的值（base），此刻已不同就 409；别的字段互不影响。

每种资源先用 GET 拿到的全部可改字段当 base 改一次：证明各接口比对用的「此刻的值」与界面拿到的输出同形（投递目标、
前置检查、配置、依赖、展开的 agent 配置都对得上），界面原样交回不会误报冲突。
"""

from typing import Any

import pytest
from pydantic import BaseModel

from muliao.api.agents import AgentFields, AgentOut, RoleFields, _editable
from muliao.api.issues import IssueFields
from muliao.api.triggers import TriggerFields
from tests.conftest import login


def _pick(out: dict[str, Any], fields: type[BaseModel]) -> dict[str, Any]:
    """全部可改字段（按模型推出，新增字段自动覆盖）此刻的值；输出里缺了哪个就在这里报 KeyError。"""
    return {f: out[f] for f in fields.model_fields}


async def _locks(client, path: str, base: dict[str, Any], field: str, other: tuple[str, Any]) -> None:
    """base 是全部可改字段此刻的值；field 是一个可改的字符串字段，other 是另一个可改字段和要改成的值。"""
    # 原样交回的 base 不误报
    resp = await client.patch(path, json={field: "theirs", "base": base})
    assert resp.status_code == 200, resp.text
    # 别人改了 field 之后，带着旧值改它：409，没写进去
    stale = await client.patch(path, json={field: "mine", "base": {field: base[field]}})
    assert stale.status_code == 409 and field in stale.json()["detail"]
    # 别的字段不受影响：带它自己的原值照常改
    name, value = other
    resp = await client.patch(path, json={name: value, "base": {name: base[name]}})
    assert resp.status_code == 200, resp.text
    # 覆盖：以对方的值为原值再存
    resp = await client.patch(path, json={field: "mine", "base": {field: "theirs"}})
    assert resp.status_code == 200, resp.text
    # base 里只能是可改的字段
    assert (await client.patch(path, json={field: "x", "base": {"bogus": 1}})).status_code == 422


@pytest.mark.usefixtures("deliveries_later")
async def test_issue_fields_are_locked_by_their_opened_values(client, make_agent):
    await login(client)
    alpha = (await make_agent()).id
    up = (await client.post("/api/issues", json={"title": "up", "owner": alpha})).json()
    issue = (
        await client.post(
            "/api/issues", json={"title": "t", "description": "d", "owner": alpha, "depends_on": [up["id"]]}
        )
    ).json()
    path = f"/api/issues/{issue['id']}"
    await _locks(client, path, _pick(issue, IssueFields), "description", ("depends_on", []))
    # 不带 base 的修改（agent 的工具、一点就改的开关）照常
    assert (await client.patch(path, json={"title": "t3"})).status_code == 200


async def test_trigger_fields_are_locked_by_their_opened_values(client, make_agent):
    await login(client)
    alpha = (await make_agent()).id
    trigger = (
        await client.post(
            "/api/triggers",
            json={
                "name": "daily",
                "agent_id": alpha,
                "kind": "schedule",
                "prompt": "p",
                "config": {"cron": "0 9 * * *"},
                "precheck": {"command": "true"},
            },
        )
    ).json()
    await _locks(
        client,
        f"/api/triggers/{trigger['id']}",
        _pick(trigger, TriggerFields),
        "prompt",
        ("config", {"cron": "0 10 * * *"}),
    )


async def test_agent_fields_are_locked_by_their_opened_values(client):
    await login(client)
    agent = (await client.post("/api/agents", json={"name": "a1", "description": "d"})).json()
    out = AgentOut.model_validate(agent)
    assert out.config is not None
    # 接口比对用的「此刻的值」正好是全部可改字段
    assert set(_editable(out, out.config)) == set(AgentFields.model_fields)
    base = _pick({**agent["config"], **agent}, AgentFields)
    await _locks(client, f"/api/agents/{agent['id']}", base, "description", ("name", "a2"))


async def test_role_fields_are_locked_by_their_opened_values(client):
    await login(client)
    role = (await client.post("/api/roles/ops", json={"description": "d", "allow": [], "deny": []})).json()
    await _locks(client, "/api/roles/ops", _pick(role, RoleFields), "description", ("deny", []))
