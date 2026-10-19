import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from muliao.errors import InvalidInput
from muliao.models import McpTransport, Origin, ReplyTo
from muliao.runtime.mcp import McpSpec
from tests.test_flow import drain, start

ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "echo_mcp.py")


def _schedule(container):
    return container.triggers, container.schedules


async def _make_due(container, trigger_id: int) -> None:
    past = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()
    async with container.db.transaction() as s:
        trigger = await container.triggers.get_in(s, trigger_id)
        container.triggers.set_state(trigger, next_run_at=past)


async def test_schedule_fires_new_session_without_notifying_and_schedules_next(container, make_agent):
    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check slack", config={"cron": "* * * * *"}
    )
    await _make_due(container, trigger.id)

    assert await schedules.fire_due() == 1

    sessions = await container.sessions.list(agent_id=alpha, origins=[Origin.TRIGGER], limit=10, offset=0)
    assert len(sessions) == 1
    assert sessions[0].origin_ref == str(trigger.id)
    turn = (await container.queue.list(status=None, session_id=sessions[0].id, limit=1))[0]
    assert turn.payload == {"prompt": "check slack", "reply_to": ReplyTo.NONE}
    trigger = await triggers.get_by_name("slack")
    assert trigger.last_session_id == sessions[0].id
    assert datetime.fromisoformat(trigger.state["next_run_at"]) > datetime.now().astimezone()


async def test_schedule_skips_fire_while_previous_run_is_unfinished(container, make_agent):
    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check", config={"cron": "* * * * *"}
    )
    await _make_due(container, trigger.id)
    await schedules.fire_due()
    await _make_due(container, trigger.id)

    await schedules.fire_due()  # 第一次的 turn 仍是 pending

    assert len(await container.sessions.list(agent_id=alpha, origins=None, limit=10, offset=0)) == 1
    await drain(container)
    await _make_due(container, trigger.id)
    await schedules.fire_due()
    assert len(await container.sessions.list(agent_id=alpha, origins=None, limit=10, offset=0)) == 2


async def test_broken_schedule_trigger_is_disabled_without_blocking_others(container, make_agent):
    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    bad = await triggers.create(
        name="bad", agent_id=alpha, kind="schedule", prompt="x", config={"cron": "* * * * *"}
    )
    good = await triggers.create(
        name="good", agent_id=alpha, kind="schedule", prompt="y", config={"cron": "* * * * *"}
    )
    for trigger_id in (bad.id, good.id):
        await _make_due(container, trigger_id)
    async with container.db.transaction() as s:
        (await triggers.get_in(s, bad.id)).prompt = "   "  # 模拟绕过校验写入的坏数据

    await schedules.fire_due()

    bad, good = await triggers.get_by_name("bad"), await triggers.get_by_name("good")
    assert not bad.enabled and "message must not be empty" in bad.last_error
    assert good.last_session_id is not None


async def test_trigger_deleted_or_paused_after_being_listed_is_skipped(container, make_agent):
    """到期列表取出之后、触发之前被删除或停用：跳过，不让进程退出，也不记成触发失败。"""
    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    gone = await triggers.create(
        name="gone", agent_id=alpha, kind="schedule", prompt="x", config={"cron": "* * * * *"}
    )
    paused = await triggers.create(
        name="paused", agent_id=alpha, kind="schedule", prompt="y", config={"cron": "* * * * *"}
    )
    for trigger_id in (gone.id, paused.id):
        await _make_due(container, trigger_id)
    real_list = triggers.list

    async def list_then_change(**kwargs):  # noqa: ANN003, ANN202
        listed = await real_list(**kwargs)
        await triggers.delete(gone.id)
        await triggers.update(paused.id, enabled=False)
        return listed

    triggers.list = list_then_change  # type: ignore[method-assign]
    try:
        assert await schedules.fire_due() == 2
    finally:
        triggers.list = real_list  # type: ignore[method-assign]

    paused = await triggers.get_by_name("paused")
    assert not paused.enabled and paused.last_error is None and paused.last_session_id is None
    assert await container.sessions.list(agent_id=alpha, origins=None, limit=10, offset=0) == []


async def test_reenabled_schedule_does_not_catch_up_on_the_paused_period(container, make_agent):
    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="daily", agent_id=alpha, kind="schedule", prompt="report", config={"cron": "0 9 * * *"}
    )
    await triggers.update(trigger.id, enabled=False)
    await _make_due(container, trigger.id)  # 暂停期间错过了一次

    trigger = await triggers.update(trigger.id, enabled=True)

    assert datetime.fromisoformat(trigger.state["next_run_at"]) > datetime.now().astimezone()
    assert await schedules.fire_due() == 0


async def test_changes_notify_every_activation_independently(container):
    first, second = container.triggers.subscribe(), container.triggers.subscribe()
    first.clear()
    second.clear()
    container.triggers._changed()
    first.clear()
    assert second.is_set()


@pytest.mark.parametrize(
    ("kind", "config"),
    [("schedule", {"cron": "every minute"}), ("schedule", {}), ("webhook", {"x": 1}), ("mq", {})],
    ids=["bad-cron", "missing-cron", "webhook-takes-no-config", "unknown-kind"],
)
async def test_trigger_config_is_validated_by_its_kind(container, make_agent, kind, config):
    alpha = (await make_agent()).id
    with pytest.raises(InvalidInput):
        await container.triggers.create(name="x", agent_id=alpha, kind=kind, prompt="p", config=config)


async def test_manual_run_starts_a_session_with_just_the_prompt(container, make_agent):
    alpha = (await make_agent()).id
    trigger = await container.triggers.create(name="h", agent_id=alpha, kind="webhook", prompt="p", config={})
    session = await container.triggers.fire(trigger.id)
    assert session.origin == Origin.TRIGGER and session.origin_ref == str(trigger.id)


async def test_mcp_add_probes_server_resolving_vault_refs(container, make_agent):
    alpha = (await make_agent()).id
    await container.vault.set(None, "ECHO_TOKEN", "tok-123")
    spec = McpSpec(
        name="echo",
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=(ECHO_SERVER,),
        env=(("TOKEN", "${vault:ECHO_TOKEN}"),),
    )

    row, tools = await container.mcp.add(alpha, spec, probe=True)

    assert sorted(tools) == ["echo", "token"]
    assert row.env == {"TOKEN": "${vault:ECHO_TOKEN}"}  # 存储的是引用而不是明文
    assert [s.name for s in await container.mcp.visible_to(alpha)] == ["echo"]
    assert await container.mcp.visible_to("beta") == []


async def test_mcp_add_failing_probe_saves_nothing(container):
    spec = McpSpec(
        name="broken",
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=("-c", "raise SystemExit(1)"),
    )

    with pytest.raises(InvalidInput, match="could not connect"):
        await container.mcp.add(None, spec, probe=True)
    assert await container.mcp.list(None, all_owners=True) == []


async def test_agent_can_call_mcp_tools_with_secret_env(container, make_agent, models):
    from tests.test_flow import call_then_answer

    alpha = (await make_agent("alpha", model="m")).id
    await container.vault.set(None, "ECHO_TOKEN", "tok-123")
    spec = McpSpec(
        name="echo",
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=(ECHO_SERVER,),
        env=(("TOKEN", "${vault:ECHO_TOKEN}"),),
    )
    await container.mcp.add(alpha, spec, probe=False)
    models.models["m"] = call_then_answer("echo_token", {}, "got it")

    session = await start(container, alpha, "token?")
    await drain(container, limit_seconds=30)

    from muliao.transcript import to_items

    items = to_items(await container.sessions.history(session.id))
    results = [i["text"] for i in items if i["type"] == "tool_result"]
    assert results == ["tok-123"]
