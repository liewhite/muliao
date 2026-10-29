import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest

from muliao.agents.api import AgentPatch, McpTransport, RoleSpec, mcp_config
from muliao.api.triggers import TriggerOut
from muliao.errors import InvalidInput
from muliao.sessions.api import Origin, SessionActivity, WorkState
from muliao.sessions.mailbox import queued_in
from muliao.sessions.tables import MailKind
from muliao.triggers.api import TriggerKindName
from muliao.triggers.service import previous_fire_state_in, set_state, trigger_in
from tests.conftest import login, owned, share
from tests.test_flow import drain, history_of, start

ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "echo_mcp.py")


def _schedule(container):
    return container.triggers, container.due_triggers


async def _make_due(container, trigger_id: int) -> None:
    past = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()
    async with container.db.transaction() as s:
        trigger = await trigger_in(s, trigger_id)
        set_state(trigger, next_run_at=past)


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
    assert sessions[0].session.origin_ref == str(trigger.id)
    async with container.db.transaction() as s:
        [mail] = await queued_in(s, sessions[0].session.id)
    # 定时触发的消息没有人在等回答：处理它的运行结束时不推送
    assert (mail.kind, mail.content, mail.awaited) == (MailKind.MESSAGE, "check slack", False)
    trigger = await triggers.get_by_name("slack")
    assert trigger.last_session_id == sessions[0].session.id
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


async def test_skipped_fires_show_in_status_and_clear_when_it_fires_again(container, make_agent):
    """跳过要看得见：连续跳过的次数、起始时间、挡路会话的状态在触发器状态里；在运行的不通知；再触发时清除。"""
    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check", config={"cron": "* * * * *"}
    )
    await _make_due(container, trigger.id)
    await schedules.fire_due()
    for _ in range(2):  # 第一次的 turn 仍是 pending
        await _make_due(container, trigger.id)
        await schedules.fire_due()

    skipped = triggers.view(await triggers.get_by_name("slack")).status.model_dump(mode="json")["skipped"]
    assert (skipped["count"], skipped["previous_state"]) == (2, "running")
    assert datetime.fromisoformat(skipped["since"]) <= datetime.now().astimezone()
    await drain(container)
    assert await container.notifications.list(10) == []

    await _make_due(container, trigger.id)
    await schedules.fire_due()
    assert (
        triggers.view(await triggers.get_by_name("slack")).status.model_dump(mode="json")["skipped"] is None
    )
    assert len(await container.sessions.list(agent_id=alpha, origins=None, limit=10, offset=0)) == 2


async def test_skipping_behind_a_waiting_session_notifies_the_user_once_per_streak(container, make_agent):
    """挡路的会话在等外面的结果（可能永远等不到，如没人回答的提问）：通知用户一次，点开是那个会话；
    同一轮连续跳过不再通知，触发过一次之后的新一轮再通知。"""
    from muliao.sessions.holds import get_in, hold_in, release

    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check", config={"cron": "* * * * *"}
    )

    async def fire_and_wait(holder: str) -> str:
        await _make_due(container, trigger.id)
        await schedules.fire_due()
        await drain(container)
        session_id = (await triggers.get_by_name("slack")).last_session_id
        async with container.db.transaction() as s:
            await hold_in(s, session_id, holder, awaited=False)
        return session_id

    async def skip_twice() -> None:
        for _ in range(2):
            await _make_due(container, trigger.id)
            await schedules.fire_due()
        await drain(container)

    first = await fire_and_wait("job:1")
    await skip_twice()

    skipped = triggers.view(await triggers.get_by_name("slack")).status.model_dump(mode="json")["skipped"]
    assert (skipped["count"], skipped["previous_state"]) == (2, "waiting")
    assert set(skipped) == {"count", "since", "previous_state"}
    notes = await container.notifications.list(10)
    assert [(n.session_id, "slack" in n.title) for n in notes] == [(first, True)]

    async with container.db.transaction() as s:
        hold = await get_in(s, first, "job:1")
        assert hold is not None
        release(hold)
    second = await fire_and_wait("job:2")
    await skip_twice()
    assert [n.session_id for n in await container.notifications.list(10)] == [second, first]


async def test_a_streak_that_turns_from_running_to_waiting_notifies_then(container, make_agent):
    from muliao.sessions.holds import hold_in

    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check", config={"cron": "* * * * *"}
    )
    await _make_due(container, trigger.id)
    await schedules.fire_due()
    await _make_due(container, trigger.id)
    await schedules.fire_due()  # 第一次的 turn 仍是 pending：在运行，不通知
    await drain(container)
    assert await container.notifications.list(10) == []
    session_id = (await triggers.get_by_name("slack")).last_session_id
    async with container.db.transaction() as s:
        await hold_in(s, session_id, "job:1", awaited=False)

    for _ in range(2):
        await _make_due(container, trigger.id)
        await schedules.fire_due()
    await drain(container)

    skipped = triggers.view(await triggers.get_by_name("slack")).status.model_dump(mode="json")["skipped"]
    assert (skipped["count"], skipped["previous_state"]) == (3, "waiting")
    assert [n.session_id for n in await container.notifications.list(10)] == [session_id]


async def test_a_new_blocking_session_or_reenabling_starts_a_new_streak(container, make_agent):
    """手动「立即运行」换了挡路的会话：新的一轮（计数重来、等待时再通知）；停用再启用也从头算。"""
    from muliao.sessions.holds import hold_in

    alpha = (await make_agent()).id
    triggers, schedules = _schedule(container)
    trigger = await triggers.create(
        name="slack", agent_id=alpha, kind="schedule", prompt="check", config={"cron": "* * * * *"}
    )

    async def wait_on(session_id: str, holder: str) -> None:
        async with container.db.transaction() as s:
            await hold_in(s, session_id, holder, awaited=False)

    async def skip() -> dict:
        await _make_due(container, trigger.id)
        await schedules.fire_due()
        await drain(container)
        return triggers.view(await triggers.get_by_name("slack")).status.model_dump(mode="json")["skipped"]

    first = await triggers.run(trigger.id)
    await drain(container)
    await wait_on(first, "job:1")
    assert (await skip())["count"] == 1
    second = await triggers.run(trigger.id)
    await drain(container)
    await wait_on(second, "job:2")

    assert (await skip())["count"] == 1
    assert [n.session_id for n in await container.notifications.list(10)] == [second, first]

    await triggers.update(trigger.id, enabled=False)
    await triggers.update(trigger.id, enabled=True)
    assert (
        triggers.view(await triggers.get_by_name("slack")).status.model_dump(mode="json")["skipped"] is None
    )


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


async def test_saving_the_same_cron_keeps_the_schedule_state(container, make_agent):
    """只改说明时界面带着原配置：不算改配置，下次时间与连续跳过的记录都留着。"""
    alpha = (await make_agent()).id
    triggers, _ = _schedule(container)
    trigger = await triggers.create(
        name="daily", agent_id=alpha, kind="schedule", prompt="report", config={"cron": "0 9 * * *"}
    )
    async with container.db.transaction() as s:
        set_state(
            await trigger_in(s, trigger.id), next_run_at="2099-01-01T09:00:00+00:00", skipped={"count": 2}
        )

    trigger = await triggers.update(trigger.id, prompt="new", config={"cron": "0 9 * * *"})

    assert trigger.prompt == "new"
    assert trigger.state == {"next_run_at": "2099-01-01T09:00:00+00:00", "skipped": {"count": 2}}


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

    async def broken_fire(_trigger_id, _payload=None):  # noqa: ANN001, ANN202
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
    await container.vault.set("ECHO_TOKEN", "tok-123", "", actor="user", owner=alpha)
    spec = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:ECHO_TOKEN}"},
    )

    row, tools = await container.mcp.add("echo", spec, actor="user", owner=alpha, probe=True)

    assert sorted(tools) == ["cwd", "echo", "screenshot", "token"]
    assert row.config.env == {"TOKEN": "${vault:ECHO_TOKEN}"}  # 存储的是引用而不是明文
    assert [s.name for s in (await container.mcp.index(alpha))] == ["echo"]
    other = (await make_agent("other")).id
    assert (await container.mcp.index(other)) == []


async def test_an_agent_uses_its_own_enabled_servers_and_those_its_roles_give_it(container, make_agent):
    alpha = (await make_agent()).id
    spec = mcp_config(transport=McpTransport.HTTP, url="http://example.invalid/mcp")
    await container.mcp.add("mine", spec, actor="user", owner=alpha, probe=False)
    await container.mcp.add("echo", spec, actor="user", owner=None, probe=False)
    await container.mcp.add("other", spec, actor="user", owner=None, probe=False)

    assert [(s.owner, s.name) for s in await container.mcp.usable(alpha)] == [(alpha, "mine")]
    assert await owned(container, "mcp", None) == ["echo", "other"]
    # 归平台的只有角色给了的生效；停用的不生效
    await container.distribution.create(RoleSpec("other-user", "d", ["mcp:other"], []), actor="user")
    await container.agents.update(alpha, AgentPatch(roles=["other-user"]), actor="user")
    assert [s.name for s in await container.mcp.index(alpha)] == ["mine", "other"]
    await container.mcp.set_enabled("mine", False, "user")
    assert [s.name for s in await container.mcp.index(alpha)] == ["other"]


async def test_mcp_add_failing_probe_saves_nothing(container):
    spec = mcp_config(
        transport=McpTransport.STDIO, command=sys.executable, args=["-c", "raise SystemExit(1)"]
    )

    with pytest.raises(InvalidInput, match="could not connect"):
        await container.mcp.add("broken", spec, actor="user", owner=None, probe=True)
    assert await owned(container, "mcp", None) == []


async def test_agent_can_call_mcp_tools_with_secret_env(container, make_agent, models):
    from tests.test_flow import call_then_answer

    alpha = (await make_agent("alpha", model="m")).id
    await container.vault.set("ECHO_TOKEN", "tok-123", "", actor="user", owner=alpha)
    spec = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:ECHO_TOKEN}"},
    )
    await container.mcp.add("echo", spec, actor="user", owner=alpha, probe=False)
    models.models["m"] = call_then_answer("echo_token", {}, "got it")

    session = await start(container, alpha, "token?")
    await drain(container, limit_seconds=30)

    from muliao.platform.tool_views import NO_RENDERS
    from muliao.sessions.transcript import to_items

    items = to_items(await history_of(container, session.id), NO_RENDERS)
    results = [i.text for i in items if i.type == "tool_result"]
    assert results == ["tok-123"]


def _same_dir(a: Path, b: Path) -> bool:
    # 临时目录可能经过符号链接（macOS 的 /var → /private/var）
    return a.samefile(b)


async def test_a_stdio_mcp_server_runs_in_the_agents_workspace(container, make_agent, models):
    from tests.test_flow import call_then_answer

    alpha = (await make_agent("alpha", model="m")).id
    spec = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add("echo", spec, actor="user", owner=alpha, probe=False)
    models.models["m"] = call_then_answer("echo_cwd", {}, "done")

    session = await start(container, alpha, "where?")
    await drain(container, limit_seconds=30)

    from muliao.platform.tool_views import NO_RENDERS
    from muliao.sessions.transcript import to_items

    items = to_items(await history_of(container, session.id), NO_RENDERS)
    [result] = [i.text for i in items if i.type == "tool_result"]
    assert _same_dir(Path(result), container.agents.workspace(alpha))


async def test_an_image_a_tool_returns_is_stored_as_a_file_and_shown_to_the_model_and_in_the_session(
    container, make_agent, models
):
    from pydantic_ai.messages import (
        BinaryContent,
        ImageUrl,
        ModelMessage,
        ModelResponse,
        TextPart,
        ToolCallPart,
    )
    from pydantic_ai.models.function import AgentInfo

    from muliao.files.api import parse_file_url
    from muliao.platform.tool_views import NO_RENDERS
    from muliao.sessions.transcript import to_items
    from tests.fixtures.echo_mcp import PNG
    from tests.test_flow import scripted, tool_returns

    alpha = (await make_agent("alpha", model="m")).id
    spec = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add("echo", spec, actor="user", owner=alpha, probe=False)
    seen = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if returns := tool_returns(messages):
            seen.extend(returns)
            return ModelResponse(parts=[TextPart("looks fine")])
        return ModelResponse(parts=[ToolCallPart("echo_screenshot", {})])

    models.models["m"] = scripted(fn)
    session = await start(container, alpha, "shoot")
    await drain(container, limit_seconds=30)

    # 模型在工具结果里拿到图片内容
    [given] = seen
    assert given.content == ["page shot", BinaryContent(PNG, media_type="image/png")]
    # 历史里只存引用，内容在文件存储里
    [stored] = [
        p for m in await history_of(container, session.id) for p in m.parts if p.part_kind == "tool-return"
    ]
    [caption, ref] = stored.content
    assert caption == "page shot" and isinstance(ref, ImageUrl)
    [meta] = await container.files.referenced([parse_file_url(ref.url)[0]])
    assert (meta.name, meta.media_type) == ("echo_screenshot.png", "image/png")
    assert container.files.read(meta) == PNG
    # 会话里图片属于这次调用的过程（images），不是给用户的文件；文字是其余的结果
    [item] = [
        i for i in to_items(await history_of(container, session.id), NO_RENDERS) if i.type == "tool_result"
    ]
    assert item.text == "page shot"
    assert item.attachments == []
    assert item.images == [
        {"url": ref.url, "name": "echo_screenshot.png", "media_type": "image/png", "inline": True}
    ]


async def test_a_tool_result_that_is_a_single_image_becomes_a_list_holding_its_file_reference(container):
    from pydantic_ai.messages import BinaryContent, ImageUrl, ToolCallPart

    from muliao.agent_runtime.attachments import ToolImagesToFiles
    from muliao.files.api import parse_file_url

    shot = BinaryContent(b"\x89PNG", media_type="image/png")
    audio = BinaryContent(b"RIFF", media_type="audio/wav")
    hook = ToolImagesToFiles(container.files)

    [ref] = await hook.after_tool_execute(
        None, call=ToolCallPart("shot", {}), tool_def=None, args={}, result=shot
    )
    assert isinstance(ref, ImageUrl)
    assert parse_file_url(ref.url)[1] == "shot.png"
    # 没有图片的结果（包括其他二进制内容）原样交回
    assert (
        await hook.after_tool_execute(None, call=ToolCallPart("a", {}), tool_def=None, args={}, result=audio)
        is audio
    )
    assert (
        await hook.after_tool_execute(None, call=ToolCallPart("a", {}), tool_def=None, args={}, result="x")
        == "x"
    )


async def test_an_mcp_tool_call_that_fails_is_handed_back_to_the_model_with_its_secrets_redacted(
    container, make_agent, models, monkeypatch
):
    """一个 server 在调用中出错（非暂时性）只是这次调用失败：模型看到脱敏后的原因，整次运行照常完成。"""
    from pydantic_ai.mcp import MCPToolset

    from muliao.platform.tables import TaskStatus
    from muliao.platform.tool_views import NO_RENDERS
    from muliao.sessions.lanes import RUN_TASK
    from muliao.sessions.transcript import to_items
    from tests.test_flow import call_then_answer, tasks_of

    async def explode(self, name, tool_args, ctx, tool):  # noqa: ANN001, ANN202
        raise RuntimeError("tool crashed holding tok-123")

    monkeypatch.setattr(MCPToolset, "call_tool", explode)
    alpha = (await make_agent("alpha", model="m")).id
    await container.vault.set("ECHO_TOKEN", "tok-123", "", actor="user", owner=alpha)
    spec = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:ECHO_TOKEN}"},
    )
    await container.mcp.add("echo", spec, actor="user", owner=alpha, probe=False)
    models.models["m"] = call_then_answer("echo_token", {}, "it failed")

    session = await start(container, alpha, "token?")
    await drain(container, limit_seconds=30)

    [run] = await tasks_of(container, RUN_TASK)
    assert run.status == TaskStatus.DONE
    items = to_items(await history_of(container, session.id), NO_RENDERS)
    [result] = [i.text for i in items if i.type == "tool_result"]
    assert "MCP server 'echo' failed" in result and "tool crashed" in result
    assert "tok-123" not in result and "[secret:ECHO_TOKEN]" in result
    assert items[-1].text == "it failed"


async def test_an_mcp_server_that_keeps_rejecting_a_call_does_not_fail_the_run(
    container, make_agent, models, monkeypatch
):
    """server 拒绝调用（MCPToolset 报 ModelRetry）与平台工具的领域错误一样计入重试预算，用完后以失败结果交还模型，
    不终止整轮运行（#145）；拒绝的文本照样脱敏。"""
    from pydantic_ai import ModelRetry
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.messages import (
        ModelMessage,
        ModelRequest,
        ModelResponse,
        RetryPromptPart,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
    )
    from pydantic_ai.models.function import AgentInfo

    from muliao.platform.tables import TaskStatus
    from muliao.sessions.lanes import RUN_TASK
    from tests.test_flow import scripted, tasks_of

    async def refuse(self, name, tool_args, ctx, tool):  # noqa: ANN001, ANN202
        raise ModelRetry("not allowed for tok-123")

    monkeypatch.setattr(MCPToolset, "call_tool", refuse)
    alpha = (await make_agent("alpha", model="m")).id
    await container.vault.set("ECHO_TOKEN", "tok-123", "", actor="user", owner=alpha)
    spec = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:ECHO_TOKEN}"},
    )
    await container.mcp.add("echo", spec, actor="user", owner=alpha, probe=False)
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        parts = last.parts if isinstance(last, ModelRequest) else []
        seen.extend(str(p.content) for p in parts if isinstance(p, RetryPromptPart | ToolReturnPart))
        if len(seen) >= 5:
            return ModelResponse(parts=[TextPart("gave up")])
        return ModelResponse(parts=[ToolCallPart("echo_token", {})])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "token?")
    await drain(container, limit_seconds=30)

    [run] = await tasks_of(container, RUN_TASK)
    assert run.status == TaskStatus.DONE
    assert len(seen) == 5
    assert all("not allowed" in t and "tok-123" not in t and "[secret:ECHO_TOKEN]" in t for t in seen)
    assert "stop retrying" in seen[3] and "stop retrying" not in seen[2]


async def test_an_mcp_server_that_fails_to_disconnect_does_not_fail_the_run(
    container, make_agent, models, monkeypatch, caplog
):
    """断开失败时工具都已用完：只记 warning，已经产出回答的运行照常完成。"""
    from pydantic_ai.mcp import MCPToolset

    from muliao.platform.tables import TaskStatus
    from muliao.sessions.lanes import RUN_TASK
    from tests.test_flow import call_then_answer, tasks_of

    aexit = MCPToolset.__aexit__

    async def fail_after_exit(self, *args):  # noqa: ANN001, ANN202
        await aexit(self, *args)
        if self._running_count == 0:  # 真正断开连接的那一次（列工具、调用时的嵌套进出不算）
            raise RuntimeError("disconnect exploded")

    monkeypatch.setattr(MCPToolset, "__aexit__", fail_after_exit)
    alpha = (await make_agent("alpha", model="m")).id
    await container.vault.set("ECHO_TOKEN", "tok-123", "", actor="user", owner=alpha)
    spec = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[ECHO_SERVER],
        env={"TOKEN": "${vault:ECHO_TOKEN}"},
    )
    await container.mcp.add("echo", spec, actor="user", owner=alpha, probe=False)
    models.models["m"] = call_then_answer("echo_token", {}, "got it")

    await start(container, alpha, "token?")
    await drain(container, limit_seconds=30)

    [run] = await tasks_of(container, RUN_TASK)
    assert run.status == TaskStatus.DONE
    [warning] = [r for r in caplog.records if r.message == "mcp server disconnect failed"]
    assert "disconnect exploded" in warning.error


@pytest.mark.parametrize(
    ("kind", "config"),
    [
        ("schedule", {"cron": "* * * * *", "tz": "UTC"}),
        ("schedule", {"cron": 5}),
        ("once", {"at": "2099-01-02T10:30", "repeat": True}),
        ("once", {}),
        ("webhook", {"secret": "x"}),
    ],
    ids=[
        "schedule-extra-key",
        "schedule-not-a-string",
        "once-extra-key",
        "once-missing-at",
        "webhook-extra-key",
    ],
)
async def test_a_config_outside_its_kinds_schema_is_refused(container, make_agent, kind, config):
    """配置只按各方式的配置类型（也是 HTTP 接口里的 config 类型）校验一遍：多余或缺少的键、类型不对都当场拒绝。"""
    alpha = (await make_agent()).id
    with pytest.raises(InvalidInput, match=f"invalid {kind} trigger config"):
        await container.triggers.create(name="bad", agent_id=alpha, kind=kind, prompt="p", config=config)


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

    await container.vault.set("API_TOKEN", "tok-secret-9", "", actor="user", owner=None)

    async def refuse(self):  # noqa: ANN001, ANN202
        raise RuntimeError("401 for https://mcp.example/sse?token=tok-secret-9")

    monkeypatch.setattr(MCPToolset, "__aenter__", refuse)
    spec = mcp_config(transport=McpTransport.HTTP, url="https://mcp.example/sse?token=${vault:API_TOKEN}")
    with pytest.raises(InvalidInput) as raised:
        await container.mcp.add("remote", spec, actor="user", owner=None, probe=True)
    assert "tok-secret-9" not in str(raised.value) and "[secret:API_TOKEN]" in str(raised.value)


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
    await container.mcp.add("echo", broken, actor="user", owner=alpha, probe=False)
    good = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add("good", good, actor="user", owner=None, probe=False)
    await share(container, "mcp:good")
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
    await container.mcp.add("broken", spec, actor="user", owner=alpha, probe=False)
    await container.mcp.add("good", spec, actor="user", owner=None, probe=False)
    await share(container, "mcp:good")
    requests, tools = _recording_model(models, "m")

    await start(container, alpha, "go")
    await drain(container, limit_seconds=30)

    [run] = await tasks_of(container, RUN_TASK)
    assert run.status == TaskStatus.DONE
    assert "good_echo" in tools and not any(t.startswith("broken_") for t in tools)
    assert "MCP server 'broken' failed" in requests[0] and "tools/list exploded" in requests[0]
    assert "mcp server unavailable" in caplog.text


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
    await container.agent_lifecycle.delete(alpha, actor="user")
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
        assert WorkState.IDLE != await previous_fire_state_in(
            s, SessionActivity(container.queue), await trigger_in(s, trigger.id)
        )


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
            return WorkState.IDLE != await previous_fire_state_in(
                s, SessionActivity(container.queue), await trigger_in(s, trigger.id)
            )

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
    assert set(listed["schedule"]["status"]) == {"next_run_at", "skipped"}
    assert listed["schedule"]["status"]["skipped"] is None
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
    await container.vault.set("MCP_TOKEN", "tok-in-url-123", "", actor="user", owner=alpha)
    spec = mcp_config(
        transport=McpTransport.HTTP, url=f"{rejecting_http_server}/sse?token=${{vault:MCP_TOKEN}}"
    )
    await container.mcp.add("remote", spec, actor="user", owner=alpha, probe=False)
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
        await container.mcp.add("remote2", spec, actor="user", owner=alpha, probe=True)
    assert "tok-in-url-123" not in str(probe.value) and "[secret:MCP_TOKEN]" in str(probe.value)
    assert "mcp probe failed" in caplog.text and "tok-in-url-123" not in caplog.text
