import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest

from muliao.agent_runtime.mcp import McpTransport, mcp_config
from muliao.api.triggers import TriggerKindName, TriggerOut
from muliao.errors import InvalidInput
from muliao.platform.tooling import Origin
from muliao.sessions.mailbox import queued_in
from muliao.sessions.tables import MailKind
from tests.conftest import login
from tests.test_flow import drain, history_of, start

ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "echo_mcp.py")


def _schedule(container):
    return container.triggers, container.due_triggers


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
    async with container.db.transaction() as s:
        [mail] = await queued_in(s, sessions[0].id)
    # 定时触发的消息没有人在等回答：处理它的运行结束时不推送
    assert (mail.kind, mail.content, mail.awaited) == (MailKind.MESSAGE, "check slack", False)
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
    changed = triggers.watch()

    await schedules.fire_due()

    bad, good = await triggers.get_by_name("bad"), await triggers.get_by_name("good")
    assert not bad.enabled and "message must not be empty" in bad.disabled_reason
    # 停用经 TriggerService，和别的修改一样通知监听者
    assert changed.is_set()
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
    assert not paused.enabled and paused.disabled_reason is None and paused.last_session_id is None
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
    first, second = container.triggers.watch(), container.triggers.watch()
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
    session = await container.sessions.get(await container.triggers.fire(trigger.id))
    assert session.origin == Origin.TRIGGER and session.origin_ref == str(trigger.id)


async def test_mcp_add_probes_server_resolving_vault_refs(container, make_agent):
    alpha = (await make_agent()).id
    await container.vault.set(None, "ECHO_TOKEN", "tok-123")
    spec = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:ECHO_TOKEN}"},
    )

    row, tools = await container.mcp.add(alpha, "echo", spec, probe=True, author="user")

    assert sorted(tools) == ["echo", "token"]
    assert row.config.env == {"TOKEN": "${vault:ECHO_TOKEN}"}  # 存储的是引用而不是明文
    assert [s.name for s in container.mcp.visible_to(alpha)] == ["echo"]
    assert container.mcp.visible_to("b2b2b2b2b2b2") == []


async def test_mcp_view_marks_public_servers_shadowed_by_enabled_private_ones(container, make_agent):
    alpha = (await make_agent()).id
    spec = mcp_config(transport=McpTransport.HTTP, url="http://example.invalid/mcp")
    for owner in (None, alpha):
        await container.mcp.add(owner, "echo", spec, probe=False, author="user")
    await container.mcp.add(None, "other", spec, probe=False, author="user")

    def view(owner):
        return [(s.owner, s.name, shadowed) for s, shadowed in container.mcp.view(owner)]

    assert view(alpha) == [(alpha, "echo", False), (None, "echo", True), (None, "other", False)]
    assert view(None) == [(None, "echo", False), (None, "other", False)]
    # 停用的私有 server 不生效，也就不遮蔽公共的：与 visible_to 一致
    await container.mcp.set_enabled(alpha, "echo", False, "user")
    assert view(alpha) == [(alpha, "echo", False), (None, "echo", False), (None, "other", False)]
    assert [(s.owner, s.name) for s in container.mcp.visible_to(alpha)] == [(None, "echo"), (None, "other")]


async def test_mcp_add_failing_probe_saves_nothing(container):
    spec = mcp_config(
        transport=McpTransport.STDIO, command=sys.executable, args=["-c", "raise SystemExit(1)"]
    )

    with pytest.raises(InvalidInput, match="could not connect"):
        await container.mcp.add(None, "broken", spec, probe=True, author="user")
    assert container.mcp.view(None) == []


async def test_agent_can_call_mcp_tools_with_secret_env(container, make_agent, models):
    from tests.test_flow import call_then_answer

    alpha = (await make_agent("alpha", model="m")).id
    await container.vault.set(None, "ECHO_TOKEN", "tok-123")
    spec = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:ECHO_TOKEN}"},
    )
    await container.mcp.add(alpha, "echo", spec, probe=False, author="user")
    models.models["m"] = call_then_answer("echo_token", {}, "got it")

    session = await start(container, alpha, "token?")
    await drain(container, limit_seconds=30)

    from muliao.sessions.transcript import to_items

    items = to_items(await history_of(container, session.id))
    results = [i.text for i in items if i.type == "tool_result"]
    assert results == ["tok-123"]


async def test_a_cron_expression_with_seconds_is_refused(container, make_agent):
    """契约是 5 段表达式；带秒的 6 段（croniter 也接受）`* * * * * *` 会每秒触发一次。"""
    from muliao.errors import InvalidInput

    alpha = (await make_agent()).id
    with pytest.raises(InvalidInput, match="5 fields"):
        await container.triggers.create(
            name="every-second", agent_id=alpha, kind="schedule", prompt="p", config={"cron": "* * * * * *"}
        )


async def test_a_failing_probe_does_not_leak_the_secrets_it_was_given(container, monkeypatch):
    """连接错误的文本可能带着已代入 secret 的 URL（如 HTTP 错误），它会交还给模型并返回给调用方。"""
    from pydantic_ai.mcp import MCPToolset

    await container.vault.set(None, "API_TOKEN", "tok-secret-9")

    async def refuse(self):  # noqa: ANN001, ANN202
        raise RuntimeError("401 for https://mcp.example/sse?token=tok-secret-9")

    monkeypatch.setattr(MCPToolset, "__aenter__", refuse)
    spec = mcp_config(transport=McpTransport.HTTP, url="https://mcp.example/sse?token=${vault:API_TOKEN}")
    with pytest.raises(InvalidInput) as raised:
        await container.mcp.add(None, "remote", spec, probe=True, author="user")
    assert "tok-secret-9" not in str(raised.value) and "[secret:API_TOKEN]" in str(raised.value)


async def test_a_temp_agent_sees_the_mcp_servers_of_its_caller(container, make_agent, models, tmp_path):
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo

    from muliao.agent_runtime.runner import RunProfile
    from tests.scripted import scripted

    parent = await make_agent("alpha", model="m")
    await container.vault.set(None, "ECHO_TOKEN", "tok-123")
    config = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:ECHO_TOKEN}"},
    )
    await container.mcp.add(parent.id, "echo", config, probe=False, author="user")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    models.models["temp"] = scripted(fn)
    answer = await container.consumer._runtime.run_temp(  # noqa: SLF001
        parent,
        task="t",
        instructions="",
        model="temp",
        profile=RunProfile(toolsets=[container.mcp.toolset()]),
        workspace=tmp_path,
        session_id=None,
    )
    assert answer.text == "ok" and {"echo_echo", "echo_token"} <= set(seen)


async def test_a_missing_vault_reference_in_an_mcp_config_fails_the_run_visibly(
    container, make_agent, models
):
    from muliao.platform.tables import TaskKind, TaskStatus
    from tests.test_flow import call_then_answer, tasks_of

    alpha = (await make_agent("alpha", model="m")).id
    config = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:MISSING}"},
    )
    await container.mcp.add(alpha, "echo", config, probe=False, author="user")
    models.models["m"] = call_then_answer("echo_token", {}, "got it")

    await start(container, alpha, "token?")
    await drain(container)

    [run] = await tasks_of(container, TaskKind.RUN)
    assert run.status == TaskStatus.FAILED and "MISSING" in (run.error or "")


async def test_a_fire_delivered_later_is_dropped_when_its_agent_was_deleted_meanwhile(container, make_agent):
    """触发记下后投信不能马上执行时由 worker 稍后投递；其间 agent 被删除，这封信没有收件人，丢弃并记日志，不留失败。"""
    from sqlalchemy import select

    from muliao.platform.tables import Task, TaskStatus
    from muliao.sessions.tables import Session

    alpha = (await make_agent("alpha")).id
    trigger = await container.triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check slack", config={"cron": "* * * * *"}
    )

    async def later(_task_id: int) -> None:
        return None

    container.triggers._run_now = later  # noqa: SLF001  模拟投递没能马上执行
    session_id = await container.triggers.fire(trigger.id)
    await container.agent_lifecycle.delete(alpha, author="user")
    await drain(container)

    async with container.db.transaction() as s:
        assert await s.get(Session, session_id) is None
        failed = (await s.scalars(select(Task).where(Task.status == TaskStatus.FAILED))).all()
    assert failed == []


async def test_a_fire_not_yet_delivered_counts_as_busy_so_the_schedule_does_not_pile_up(
    container, make_agent
):
    alpha = (await make_agent("alpha")).id
    trigger = await container.triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check slack", config={"cron": "* * * * *"}
    )

    async def later(_task_id: int) -> None:
        return None

    container.triggers._run_now = later  # noqa: SLF001  模拟投递没能马上执行
    await container.triggers.fire(trigger.id)
    async with container.db.transaction() as s:
        assert await container.triggers.busy_in(s, await container.triggers.get_in(s, trigger.id))


async def test_a_previous_fire_waiting_on_a_background_job_counts_as_busy(container, make_agent):
    """上一次触发的会话停了，但还在等后台任务（active 占用）：结果回来会接着做，这次不再触发；占用释放后空闲。"""
    from muliao.sessions.holds import hold_in, release_in

    alpha = (await make_agent("alpha")).id
    trigger = await container.triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check slack", config={"cron": "* * * * *"}
    )
    session_id = await container.triggers.fire(trigger.id)
    await drain(container)

    async def busy() -> bool:
        async with container.db.transaction() as s:
            return await container.triggers.busy_in(s, await container.triggers.get_in(s, trigger.id))

    assert not await busy()
    async with container.db.transaction() as s:
        await hold_in(s, session_id, "job:1", awaited=False)
    assert await busy()
    async with container.db.transaction() as s:
        await release_in(s, session_id, "job:1")
    assert not await busy()


async def test_http_trigger_types_cover_exactly_the_registered_kinds(container):
    assert set(get_args(TriggerKindName)) == set(container.triggers.kinds)
    # 联合类型里每种方式恰有一个成员，列表接口才不会因为漏了某种方式而 500
    members = get_args(get_args(TriggerOut)[0])
    assert sorted(get_args(m.model_fields["kind"].annotation)[0] for m in members) == sorted(
        get_args(TriggerKindName)
    )


async def test_trigger_list_returns_each_kinds_typed_status(client, container, make_agent):
    alpha = (await make_agent()).id
    for name, kind, config in [
        ("s", "schedule", {"cron": "0 9 * * *"}),
        ("o", "once", {"at": "2099-01-01T10:00"}),
        ("w", "webhook", {}),
    ]:
        await container.triggers.create(name=name, agent_id=alpha, kind=kind, prompt="p", config=config)
    await login(client)
    listed = {t["kind"]: t for t in (await client.get("/api/triggers")).json()}
    assert set(listed["schedule"]["status"]) == {"next_run_at"}
    assert listed["once"]["status"]["run_at"].startswith("2099-01-01T10:00")
    assert listed["webhook"]["status"] == {"url": "http://test/api/hooks/w", "has_token": False}


async def test_disabling_a_trigger_notifies_watchers_and_reports_a_deleted_one(container, make_agent):
    alpha = (await make_agent()).id
    t = await container.triggers.create(name="t", agent_id=alpha, kind="webhook", prompt="p", config={})
    changed = container.triggers.watch()

    assert await container.triggers.disable(t.id, "boom")
    assert changed.is_set()
    disabled = await container.triggers.get_by_name("t")
    assert not disabled.enabled and disabled.disabled_reason == "boom"

    await container.triggers.delete(t.id)
    assert not await container.triggers.disable(t.id, "boom")
