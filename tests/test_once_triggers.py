"""一次性触发器（kind = once）：到点触发一次就归档——不再出现在列表里，但历史查得到。

归档是触发器领域的通用概念（finished_at）：归档的不能再修改、启用或立即运行，名称让给新的触发器，
它发起过的会话仍然按它的 id 查得到。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from muliao.errors import Conflict, InvalidInput
from muliao.platform.tooling import Origin
from muliao.triggers.once import OnceKind
from muliao.triggers.precheck import Precheck
from tests.conftest import login
from tests.test_flow import deliver


def _later(hours: int = 1) -> str:
    return (datetime.now().astimezone() + timedelta(hours=hours)).isoformat()


async def _once(container, agent_id: str, *, name: str = "remind", **kwargs) -> object:
    return await container.triggers.create(
        name=name, agent_id=agent_id, kind="once", prompt="remind me", config={"at": _later()}, **kwargs
    )


async def _make_due(container, trigger_id: int) -> None:
    """把预定时间改到过去，模拟到点（创建时不接受过去的时间）。"""
    past = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()
    async with container.db.transaction() as s:
        trigger = await container.triggers.get_in(s, trigger_id)
        trigger.config = {**trigger.config, "at": past}


async def _fire_due(container, trigger_id: int) -> int:
    await _make_due(container, trigger_id)
    return await container.due_triggers.fire_due()


def _workspace(container, agent_id: str):  # noqa: ANN202
    workspace = container.agents.workspace(agent_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


async def test_once_fires_at_its_time_and_is_then_archived_with_its_session(container, make_agent):
    alpha = (await make_agent()).id
    trigger = await _once(container, alpha)

    assert await _fire_due(container, trigger.id) == 1
    assert await container.due_triggers.fire_due() == 0  # 归档之后不再扫描到它

    assert await container.triggers.list() == []
    [archived] = await container.triggers.list(finished=True)
    assert archived.id == trigger.id and archived.finished_at is not None
    sessions = await container.sessions.list(
        agent_id=None, origins=[Origin.TRIGGER], origin_ref=str(trigger.id), limit=10, offset=0
    )
    assert [s.id for s in sessions] == [archived.last_session_id]


async def test_a_once_trigger_in_the_past_is_refused_at_creation(container, make_agent):
    alpha = (await make_agent()).id
    with pytest.raises(InvalidInput, match="in the past"):
        await container.triggers.create(
            name="late", agent_id=alpha, kind="once", prompt="p", config={"at": "2020-01-01T09:00"}
        )


def test_a_once_time_without_an_offset_is_read_in_the_server_timezone():
    kind = OnceKind(ZoneInfo("Asia/Shanghai"))

    config, state = kind.configure({"at": " 2099-01-02T10:30 "})

    # 到期时间就是配置本身，没有随运行变化的状态
    assert (config, state) == ({"at": "2099-01-02T10:30:00+08:00"}, {})


@pytest.mark.parametrize("at", ["next tuesday", "2099-13-40T00:00"], ids=["not-a-time", "impossible-date"])
def test_a_once_time_that_is_not_iso_8601_is_refused(at):
    with pytest.raises(InvalidInput, match="expected ISO 8601"):
        OnceKind(ZoneInfo("UTC")).configure({"at": at})


async def test_an_archived_trigger_leaves_its_name_to_a_new_one(container, make_agent):
    alpha = (await make_agent()).id
    first = await _once(container, alpha, name="daily-check")
    await _fire_due(container, first.id)

    second = await _once(container, alpha, name="daily-check")

    assert second.id != first.id
    assert [t.id for t in await container.triggers.list()] == [second.id]
    # 按名称找到的是还会触发的那个；归档的那个仍在历史里
    assert (await container.triggers.get_by_name("daily-check")).id == second.id
    assert [t.id for t in await container.triggers.list(finished=True)] == [first.id]


async def test_an_archived_trigger_cannot_be_changed_enabled_or_run_but_can_be_deleted(container, make_agent):
    alpha = (await make_agent()).id
    trigger = await _once(container, alpha)
    await _fire_due(container, trigger.id)

    for change in (
        container.triggers.update(trigger.id, prompt="other"),
        container.triggers.update(trigger.id, enabled=True),
        container.triggers.run(trigger.id),
        container.triggers.get_by_name("remind"),
    ):
        with pytest.raises(Conflict, match="has finished"):
            await change

    await container.triggers.delete(trigger.id)
    assert await container.triggers.list(finished=True) == []


async def test_a_once_trigger_is_archived_even_when_its_precheck_finds_nothing_to_do(container, make_agent):
    alpha = (await make_agent()).id
    _workspace(container, alpha)
    trigger = await _once(container, alpha, precheck=Precheck(command="true"))

    await _fire_due(container, trigger.id)

    archived = (await container.triggers.list(finished=True))[0]
    assert archived.finished_at is not None and archived.last_session_id is None


async def test_a_once_trigger_whose_precheck_fails_is_archived_with_the_error(container, make_agent):
    alpha = (await make_agent()).id
    _workspace(container, alpha)
    trigger = await _once(container, alpha, precheck=Precheck(command="echo boom >&2; exit 3"))

    await _fire_due(container, trigger.id)
    await deliver(container)

    archived = (await container.triggers.list(finished=True))[0]
    assert archived.finished_at is not None and "exited with 3" in archived.precheck_error


async def test_a_once_trigger_that_cannot_fire_is_archived_with_the_reason(container, make_agent):
    alpha = (await make_agent()).id
    trigger = await _once(container, alpha)
    async with container.db.transaction() as s:
        (await container.triggers.get_in(s, trigger.id)).prompt = "   "  # 模拟绕过校验写入的坏数据

    await _fire_due(container, trigger.id)

    archived = (await container.triggers.list(finished=True))[0]
    assert archived.finished_at is not None and not archived.enabled
    assert "message must not be empty" in archived.disabled_reason


async def test_running_a_once_trigger_by_hand_does_not_use_up_its_firing(container, make_agent):
    alpha = (await make_agent()).id
    trigger = await _once(container, alpha)

    manual = await container.triggers.run(trigger.id)

    assert manual is not None
    assert (await container.triggers.get_by_name("remind")).finished_at is None
    assert await _fire_due(container, trigger.id) == 1
    archived = (await container.triggers.list(finished=True))[0]
    assert archived.finished_at is not None and archived.last_session_id != manual
    history = await container.sessions.list(
        agent_id=None, origins=[Origin.TRIGGER], origin_ref=str(trigger.id), limit=10, offset=0
    )
    assert {s.id for s in history} == {manual, archived.last_session_id}


async def test_the_api_lists_the_archived_ones_separately_with_the_sessions_they_started(
    client, container, make_agent
):
    alpha = (await make_agent()).id
    await login(client)
    trigger = await _once(container, alpha)
    await _fire_due(container, trigger.id)
    still_running = await _once(container, alpha, name="later")

    listed = (await client.get("/api/triggers")).json()
    archived = (await client.get("/api/triggers", params={"finished": True})).json()

    assert [t["id"] for t in listed] == [still_running.id]
    assert [(t["id"], t["finished_at"] is not None) for t in archived] == [(trigger.id, True)]
    history = await client.get("/api/sessions", params={"origin": ["trigger"], "origin_ref": str(trigger.id)})
    assert [s["id"] for s in history.json()] == [archived[0]["last_session_id"]]
    assert (await client.post(f"/api/triggers/{trigger.id}/run")).status_code == 409
    assert (await client.patch(f"/api/triggers/{trigger.id}", json={"enabled": True})).status_code == 409
    assert (await client.delete(f"/api/triggers/{trigger.id}")).status_code == 204


async def test_the_agent_creates_a_once_trigger_and_later_looks_up_what_came_of_it(
    container, make_agent, models
):
    from tests.test_tools import run_tools

    me = await make_agent("manager", model="tools", reflect_every=100)

    created = await run_tools(
        container,
        models,
        me.id,
        [
            (
                "trigger_create",
                {"name": "remind", "kind": "once", "prompt": "Remind me.", "config": {"at": _later()}},
            )
        ],
    )
    trigger = await container.triggers.get_by_name("remind")
    await _fire_due(container, trigger.id)
    outputs = await run_tools(
        container,
        models,
        me.id,
        [("trigger_list", {}), ("trigger_list", {"finished": True}), ("trigger_run", {"name": "remind"})],
    )

    # 同一个步骤遇到 ModelRetry 会再问一次模型，结果重复一遍：按出现顺序去重
    listed, archived, rerun = list(dict.fromkeys(outputs))
    assert "run_at" in created[0]
    assert listed == "[]"  # 跑完就不在列表里了
    assert "remind" in archived and "finished_at" in archived
    assert str((await container.triggers.list(finished=True))[0].last_session_id) in archived
    # 归档的按名称指不了，但错误说清楚是跑完归档了，不是找不到
    assert "has finished" in rerun
    # 按名称删除同样指不到归档的；提示不能自相矛盾地说「可以删除」
    (deleted,) = set(await run_tools(container, models, me.id, [("trigger_delete", {"name": "remind"})]))
    assert "has finished" in deleted and "nothing to change, run or delete" in deleted
    assert "can only be viewed or deleted" not in deleted
