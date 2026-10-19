import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from muliao.errors import Forbidden, InvalidInput
from muliao.models import CronJob, DeliveryChannel, McpTransport, Origin, Task, TaskKind
from muliao.runtime.mcp import McpSpec
from muliao.telegram import split_message
from muliao.triggers.telegram import TelegramTrigger
from tests.test_flow import drain

ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "echo_mcp.py")


async def _make_due(container, job_id: int) -> None:
    async with container.db.transaction() as s:
        (await s.get(CronJob, job_id)).next_run_at = datetime.now().astimezone() - timedelta(seconds=1)


async def test_cron_fires_new_session_without_delivery_and_schedules_next(container, make_agent):
    alpha = (await make_agent()).id
    job = await container.cron.create(
        name="slack", agent_id=alpha, schedule="* * * * *", prompt="check slack"
    )
    await _make_due(container, job.id)

    assert await container.cron.fire_due() == 1

    sessions = await container.sessions.list(agent_id=alpha, origins=[Origin.CRON], limit=10, offset=0)
    assert len(sessions) == 1
    assert sessions[0].delivery == DeliveryChannel.NONE and sessions[0].origin_ref == str(job.id)
    job = await container.cron.get_by_name("slack")
    assert job.last_session_id == sessions[0].id
    assert job.next_run_at > datetime.now().astimezone()


async def test_cron_skips_fire_while_previous_run_is_unfinished(container, make_agent):
    alpha = (await make_agent()).id
    job = await container.cron.create(name="slack", agent_id=alpha, schedule="* * * * *", prompt="check")
    await _make_due(container, job.id)
    await container.cron.fire_due()
    await _make_due(container, job.id)

    await container.cron.fire_due()  # 第一次的 turn 仍是 pending

    assert len(await container.sessions.list(agent_id=alpha, origins=None, limit=10, offset=0)) == 1
    await drain(container)
    await _make_due(container, job.id)
    await container.cron.fire_due()
    assert len(await container.sessions.list(agent_id=alpha, origins=None, limit=10, offset=0)) == 2


async def test_cron_rejects_invalid_expression(container, make_agent):
    alpha = (await make_agent()).id
    with pytest.raises(InvalidInput):
        await container.cron.create(name="x", agent_id=alpha, schedule="every minute", prompt="p")


async def test_webhook_requires_matching_token_and_wraps_event(container, make_agent):
    alpha = (await make_agent()).id
    hook = await container.webhooks.create(name="github", agent_id=alpha, prompt="Triage this event.")

    with pytest.raises(Forbidden):
        await container.webhooks.receive(hook.id, "wrong", "{}")
    session = await container.webhooks.receive(hook.id, hook.token, '{"action":"opened"}')

    async with container.db.transaction() as s:
        task = await s.scalar(select(Task).where(Task.session_id == session.id))
    assert task.payload["prompt"] == 'Triage this event.\n\n<event>\n{"action":"opened"}\n</event>'
    assert session.delivery == DeliveryChannel.WEBPUSH


async def test_mcp_add_probes_server_resolving_vault_refs(container, make_agent):
    alpha = (await make_agent()).id
    await container.vault.set("ECHO_TOKEN", "tok-123")
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
    await container.vault.set("ECHO_TOKEN", "tok-123")
    spec = McpSpec(
        name="echo",
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=(ECHO_SERVER,),
        env=(("TOKEN", "${vault:ECHO_TOKEN}"),),
    )
    await container.mcp.add(alpha, spec, probe=False)
    models.models["m"] = call_then_answer("echo_token", {}, "got it")

    session = await container.sessions.start(
        agent_id=alpha,
        prompt="token?",
        origin=Origin.USER,
        origin_ref=None,
        delivery=DeliveryChannel.NONE,
    )
    await drain(container, limit_seconds=30)

    from muliao.transcript import to_items

    items = to_items(await container.sessions.history(session.id))
    results = [i["text"] for i in items if i["type"] == "tool_result"]
    assert results == ["tok-123"]


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


async def test_telegram_reuses_chat_session_and_drops_unknown_chats(container, make_agent):
    alpha = (await make_agent()).id
    tg = FakeTelegram()
    trigger = TelegramTrigger(tg, container.sessions, container.notifier, alpha, {42})  # type: ignore[arg-type]

    def update(chat: int, text: str, uid: int) -> dict:
        return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}

    await trigger.handle(update(42, "hello", 1))
    await trigger.handle(update(42, "again", 2))
    await trigger.handle(update(7, "intruder", 3))

    sessions = await container.sessions.list(agent_id=alpha, origins=[Origin.TELEGRAM], limit=10, offset=0)
    assert len(sessions) == 1
    assert sessions[0].delivery == DeliveryChannel.TELEGRAM and sessions[0].delivery_target == "42"
    async with container.db.transaction() as s:
        prompts = [
            t.payload["prompt"]
            for t in (await s.scalars(select(Task).where(Task.kind == TaskKind.TURN))).all()
        ]
    assert prompts == ["hello", "again"]

    await trigger.handle(update(42, "/new", 4))
    assert (
        len(await container.sessions.list(agent_id=alpha, origins=[Origin.TELEGRAM], limit=10, offset=0)) == 2
    )


def test_split_message_respects_limit_and_prefers_newlines():
    text = "a" * 5 + "\n" + "b" * 5
    assert split_message(text, limit=8) == ["aaaaa", "bbbbb"]
    assert split_message("x" * 10, limit=4) == ["xxxx", "xxxx", "xx"]
