import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest

from muliao.agents.api import McpTransport, mcp_config
from muliao.api.triggers import TriggerKindName, TriggerOut
from muliao.errors import InvalidInput
from muliao.sessions.api import Origin
from muliao.sessions.mailbox import queued_in
from muliao.sessions.tables import MailKind
from muliao.triggers.service import trigger_in
from tests.conftest import login
from tests.test_flow import drain, history_of, start

ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "echo_mcp.py")


def _schedule(container):
    return container.triggers, container.due_triggers


async def _make_due(container, trigger_id: int) -> None:
    past = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()
    async with container.db.transaction() as s:
        trigger = await trigger_in(s, trigger_id)
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
        (await trigger_in(s, bad.id)).prompt = "   "  # 模拟绕过校验写入的坏数据
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
    ("kind", "corrupt"),
    [
        ("schedule", {"state": {}}),
        ("schedule", {"state": {"next_run_at": "soon"}}),
        ("schedule", {"state": {"next_run_at": "2000-01-01T00:00"}}),
        ("schedule", {"config": {}}),
        ("once", {"config": {}}),
        ("once", {"config": {"at": "2000-01-01T00:00"}}),
    ],
    ids=[
        "schedule-without-next-run",
        "schedule-with-unparsable-next-run",
        "schedule-next-run-without-timezone",
        "schedule-without-cron",
        "once-without-time",
        "once-time-without-timezone",
    ],
)
async def test_a_trigger_with_corrupt_stored_data_is_disabled_and_the_scan_goes_on(
    container, make_agent, kind, corrupt
):
    """坏的一行（绕过校验写入、或旧版本留下的）只停用它自己，不让扫描循环崩掉、拖累别的触发器。"""
    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    config = {"cron": "* * * * *"} if kind == "schedule" else {"at": "2099-01-01T00:00"}
    bad = await triggers.create(name="bad", agent_id=alpha, kind=kind, prompt="x", config=config)
    good = await triggers.create(
        name="good", agent_id=alpha, kind="schedule", prompt="y", config={"cron": "* * * * *"}
    )
    await _make_due(container, good.id)
    async with container.db.transaction() as s:
        row = await trigger_in(s, bad.id)
        if kind == "schedule" and "config" in corrupt:
            # 到期了才会去读 cron：先让它到期，再弄坏配置
            row.state = {"next_run_at": (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()}
        for column, value in corrupt.items():
            setattr(row, column, value)

    assert await schedules.fire_due() >= 1
    await schedules._next_wake()

    bad, good = await triggers.get_by_name("bad"), await triggers.get_by_name("good")
    assert not bad.enabled and bad.disabled_reason.startswith(f"CorruptState: trigger {bad.id} (bad)")
    assert good.last_session_id is not None


async def test_a_trigger_corrupted_after_being_listed_is_disabled_when_it_fires(container, make_agent):
    """列出到期之后、触发之前才被改坏的：按最新的行重新判断时发现，同样只停用它。"""
    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="bad", agent_id=alpha, kind="schedule", prompt="x", config={"cron": "* * * * *"}
    )
    async with container.db.transaction() as s:
        (await trigger_in(s, trigger.id)).state = {}

    await schedules._fire_one(trigger.id, datetime.now().astimezone())

    trigger = await triggers.get_by_name("bad")
    assert not trigger.enabled and trigger.disabled_reason.startswith(
        f"CorruptState: trigger {trigger.id} (bad)"
    )
    assert trigger.last_session_id is None


async def test_corrupt_state_outside_the_trigger_itself_is_not_blamed_on_it(
    container, make_agent, monkeypatch
):
    """触发链路上别处的数据坏了（不是这个触发器自己存的）按程序错误上抛，不停用触发器。"""
    from muliao.errors import CorruptState

    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="ok", agent_id=alpha, kind="schedule", prompt="x", config={"cron": "* * * * *"}
    )
    await _make_due(container, trigger.id)

    async def broken_fire(_trigger_id):  # noqa: ANN001, ANN202
        raise CorruptState("agent resources missing")

    monkeypatch.setattr(triggers, "fire", broken_fire)
    with pytest.raises(ExceptionGroup) as raised:
        await schedules.fire_due()
    assert raised.group_contains(CorruptState, match="agent resources missing")
    assert (await triggers.get_by_name("ok")).enabled


@pytest.mark.parametrize(
    ("kind", "config"),
    [
        ("schedule", {"cron": "every minute"}),
        ("schedule", {"cron": "0 0 30 2 *"}),
        ("schedule", {}),
        ("webhook", {"x": 1}),
        ("mq", {}),
    ],
    ids=["bad-cron", "cron-that-never-fires", "missing-cron", "webhook-takes-no-config", "unknown-kind"],
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
        profile=RunProfile(toolsets=[container.mcp_tools]),
        workspace=tmp_path,
        session_id=None,
    )
    assert answer.text == "ok" and {"echo_echo", "echo_token"} <= set(seen)


def _recording_model(models, name: str) -> tuple[list[str], list[str]]:
    """回答 ok 的模型，记下每次请求的最后一条消息与可用的工具名。"""
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo

    from tests.scripted import scripted

    requests: list[str] = []
    tools: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(str(messages[-1]))
        tools.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    models.models[name] = scripted(fn)
    return requests, tools


async def test_an_mcp_server_with_a_missing_secret_is_skipped_and_the_model_is_told(
    container, make_agent, models, caplog
):
    """一个 server 引用的 secret 不在了：这次运行没有它的工具，别的 server 照常可用，模型从运行上下文得知原因。"""
    from muliao.platform.tables import TaskStatus
    from muliao.sessions.lanes import RUN_TASK
    from tests.test_flow import tasks_of

    alpha = (await make_agent("alpha", model="m")).id
    broken = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:MISSING}"},
    )
    await container.mcp.add(alpha, "echo", broken, probe=False, author="user")
    good = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add(None, "good", good, probe=False, author="user")
    requests, tools = _recording_model(models, "m")

    await start(container, alpha, "token?")
    await drain(container)

    [run] = await tasks_of(container, RUN_TASK)
    assert run.status == TaskStatus.DONE
    assert "good_echo" in tools and not any(t.startswith("echo_") for t in tools)
    assert "MCP server 'echo'" in requests[0] and "MISSING" in requests[0]
    assert "mcp server unavailable" in caplog.text


async def test_an_mcp_server_that_fails_to_list_its_tools_is_skipped_and_the_model_is_told(
    container, make_agent, models, caplog, monkeypatch
):
    """握手正常、列工具（tools/list）失败：与连不上一样只跳过它，不在请求模型时拖垮整次运行。"""
    from pydantic_ai.mcp import MCPToolset

    from muliao.platform.tables import TaskStatus
    from muliao.sessions.lanes import RUN_TASK
    from tests.test_flow import tasks_of

    list_tools = MCPToolset.list_tools

    async def fail_for_broken(self):  # noqa: ANN001, ANN202
        if "broken" in (self.id or ""):
            raise RuntimeError("tools/list exploded")
        return await list_tools(self)

    monkeypatch.setattr(MCPToolset, "list_tools", fail_for_broken)
    alpha = (await make_agent("alpha", model="m")).id
    spec = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add(alpha, "broken", spec, probe=False, author="user")
    await container.mcp.add(None, "good", spec, probe=False, author="user")
    requests, tools = _recording_model(models, "m")

    await start(container, alpha, "go")
    await drain(container, limit_seconds=30)

    [run] = await tasks_of(container, RUN_TASK)
    assert run.status == TaskStatus.DONE
    assert "good_echo" in tools and not any(t.startswith("broken_") for t in tools)
    assert "MCP server 'broken' failed" in requests[0] and "tools/list exploded" in requests[0]
    assert "mcp server unavailable" in caplog.text


async def test_a_corrupt_public_mcp_config_is_reported_to_a_temp_agent_without_failing_it(
    container, make_agent, models, tmp_path
):
    from muliao.agent_runtime.runner import RunProfile

    parent = await make_agent("alpha", model="m")
    good = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add(None, "good", good, probe=False, author="user")
    (container.resources.owner_dir(None) / "mcp" / "bad.json").write_text("{not json")
    requests, tools = _recording_model(models, "temp")

    answer = await container.consumer._runtime.run_temp(  # noqa: SLF001
        parent,
        task="the task",
        instructions="",
        model="temp",
        profile=RunProfile(toolsets=[container.mcp_tools]),
        workspace=tmp_path,
        session_id=None,
    )

    assert answer.text == "ok" and "good_echo" in tools
    assert (
        "public MCP server 'bad' is corrupt and cannot be used until it is removed and added again"
        in requests[0]
    )
    assert "the task" in requests[0]


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
        assert await container.triggers.busy_in(s, await trigger_in(s, trigger.id))


async def test_a_previous_fire_waiting_on_a_background_job_counts_as_busy(container, make_agent):
    """上一次触发的会话停了，但还在等后台任务（active 占用）：结果回来会接着做，这次不再触发；占用释放后空闲。"""
    from muliao.sessions.holds import get_in, hold_in, release

    alpha = (await make_agent("alpha")).id
    trigger = await container.triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check slack", config={"cron": "* * * * *"}
    )
    session_id = await container.triggers.fire(trigger.id)
    await drain(container)

    async def busy() -> bool:
        async with container.db.transaction() as s:
            return await container.triggers.busy_in(s, await trigger_in(s, trigger.id))

    assert not await busy()
    async with container.db.transaction() as s:
        await hold_in(s, session_id, "job:1", awaited=False)
    assert await busy()
    async with container.db.transaction() as s:
        hold = await get_in(s, session_id, "job:1")
        assert hold is not None
        release(hold)
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


@pytest.fixture
def rejecting_http_server():
    """所有请求都回 401 的 HTTP server：SSE 传输（URL 以 /sse 结尾）的客户端会抛出带完整 URL 的 HTTPStatusError。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Reject(BaseHTTPRequestHandler):
        def _reject(self):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_DELETE = _reject

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Reject)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


async def test_an_unreachable_mcp_server_is_skipped_and_keeps_its_secrets_out_of_the_context_and_logs(
    container, make_agent, models, rejecting_http_server, caplog, monkeypatch
):
    import logging

    from muliao.logging_setup import quiet_http_clients
    from muliao.platform.tables import TaskStatus
    from muliao.sessions.lanes import RUN_TASK
    from tests.test_flow import tasks_of

    # 与服务一样：INFO 级别，HTTP 客户端的逐请求日志关掉（它们带完整 URL）
    caplog.set_level(logging.INFO)
    for name in ("httpx", "httpx2"):
        # 测试结束后恢复原级别
        monkeypatch.setattr(logging.getLogger(name), "level", logging.getLogger(name).level)
    quiet_http_clients()
    alpha = (await make_agent("alpha", model="m")).id
    await container.vault.set(None, "MCP_TOKEN", "tok-in-url-123")
    spec = mcp_config(
        transport=McpTransport.HTTP, url=f"{rejecting_http_server}/sse?token=${{vault:MCP_TOKEN}}"
    )
    await container.mcp.add(alpha, "remote", spec, probe=False, author="user")
    requests, tools = _recording_model(models, "m")

    await start(container, alpha, "go", origin=Origin.TRIGGER)
    await drain(container, limit_seconds=30)

    # 连不上的 server 只是这次没有它的工具，运行照常完成；告诉模型的原因与日志都已脱敏
    [run] = await tasks_of(container, RUN_TASK)
    assert run.status == TaskStatus.DONE and not any(t.startswith("remote_") for t in tools)
    assert "MCP server 'remote' failed" in requests[0] and "[secret:MCP_TOKEN]" in requests[0]
    assert "tok-in-url-123" not in requests[0]
    assert "mcp server unavailable" in caplog.text and "tok-in-url-123" not in caplog.text

    # 添加时的探测走同一个脱敏入口
    with pytest.raises(InvalidInput, match="could not connect") as probe:
        await container.mcp.add(alpha, "remote2", spec, probe=True, author="user")
    assert "tok-in-url-123" not in str(probe.value) and "[secret:MCP_TOKEN]" in str(probe.value)
    assert "mcp probe failed" in caplog.text and "tok-in-url-123" not in caplog.text
