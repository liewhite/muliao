import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from muliao.errors import InvalidInput
from muliao.models import CronJob, McpTransport, Origin, ReplyTo
from muliao.runtime.mcp import McpSpec
from tests.test_flow import drain

ECHO_SERVER = str(Path(__file__).parent / "fixtures" / "echo_mcp.py")


async def _make_due(container, job_id: int) -> None:
    async with container.db.transaction() as s:
        (await s.get(CronJob, job_id)).next_run_at = datetime.now().astimezone() - timedelta(seconds=1)


async def test_cron_fires_new_session_without_notifying_and_schedules_next(container, make_agent):
    alpha = (await make_agent()).id
    job = await container.cron.create(
        name="slack", agent_id=alpha, schedule="* * * * *", prompt="check slack"
    )
    await _make_due(container, job.id)

    assert await container.cron.fire_due() == 1

    sessions = await container.sessions.list(agent_id=alpha, origins=[Origin.CRON], limit=10, offset=0)
    assert len(sessions) == 1
    assert sessions[0].origin_ref == str(job.id)
    turn = (await container.queue.list(status=None, session_id=sessions[0].id, limit=1))[0]
    assert turn.payload["reply_to"] == ReplyTo.NONE
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
        reply_to=ReplyTo.USER,
    )
    await drain(container, limit_seconds=30)

    from muliao.transcript import to_items

    items = to_items(await container.sessions.history(session.id))
    results = [i["text"] for i in items if i["type"] == "tool_result"]
    assert results == ["tok-123"]
