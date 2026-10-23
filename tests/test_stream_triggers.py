"""流式触发：平台托管常驻命令，stdout 每行一个事件（triggers/stream.py）。"""

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import anyio
import pytest
from pydantic_ai.messages import UserPromptPart

from muliao.errors import InvalidInput
from muliao.sessions.api import Origin, SessionActivity
from muliao.sessions.mailbox import queued_in
from muliao.triggers.service import EVENT_MAX_BYTES
from muliao.triggers.stream import StreamRunner
from tests.test_flow import drain, history_of


def _runner(container, **kw) -> StreamRunner:
    return StreamRunner(
        container.db,
        container.triggers,
        SessionActivity(container.queue),
        container.vault,
        container.agents.workspace,
        container.messaging,
        kw.pop("max_bytes", EVENT_MAX_BYTES),
        backoff=kw.pop("backoff", lambda _n: 0.05),
        stable_seconds=kw.pop("stable_seconds", 60.0),
        busy_poll_seconds=kw.pop("busy_poll_seconds", 0.05),
    )


@asynccontextmanager
async def _running(runner: StreamRunner) -> AsyncIterator[None]:
    async with anyio.create_task_group() as group:
        group.start_soon(runner.run_forever)
        try:
            yield
        finally:
            group.cancel_scope.cancel()


async def _until(check: Callable, seconds: float = 10.0) -> None:
    with anyio.fail_after(seconds):
        while not await check():  # noqa: ASYNC110  轮询外部状态（数据库、进程），没有可等的事件
            await anyio.sleep(0.05)


async def _sessions(container, agent_id: str):
    return await container.sessions.list(agent_id=agent_id, origins=[Origin.TRIGGER], limit=20, offset=0)


async def _contents(container, agent_id: str) -> list[str]:
    out = []
    for item in reversed(await _sessions(container, agent_id)):
        async with container.db.transaction() as s:
            out += [m.content for m in await queued_in(s, item.session.id)]
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _create(container, agent_id: str, command: str, **config):
    return await container.triggers.create(
        name="feed",
        agent_id=agent_id,
        kind="stream",
        prompt="handle",
        config={"command": command, **config},
    )


@pytest.mark.parametrize(
    "config",
    [{}, {"command": ""}, {"command": "x", "debounce_seconds": -1}, {"command": "x", "unknown": 1}],
)
async def test_a_stream_config_is_validated(container, make_agent, config):
    with pytest.raises(InvalidInput):
        await container.triggers.create(
            name="feed", agent_id=(await make_agent()).id, kind="stream", prompt="p", config=config
        )


async def test_lines_within_the_debounce_window_fire_one_session_with_the_lines_as_the_event(
    container, make_agent
):
    alpha = (await make_agent()).id
    await _create(container, alpha, "printf 'a\\n\\nb\\n'; sleep 30", debounce_seconds=0.3)

    async with _running(_runner(container)):
        await _until(lambda: _has_sessions(container, alpha, 1))
        await anyio.sleep(0.3)

    assert await _contents(container, alpha) == ["handle\n\n<event>\na\nb\n</event>"]
    status = container.triggers.status_of(await container.triggers.get_by_name("feed"))
    assert status["running_since"] is not None
    assert status["stderr_log"] == "trigger-logs/feed.stderr.log"


async def _has_sessions(container, agent_id: str, n: int) -> bool:
    return len(await _sessions(container, agent_id)) >= n


async def test_events_queue_while_the_previous_session_is_busy_and_are_delivered_together_after(
    container, make_agent
):
    alpha = (await make_agent()).id
    events = container.agents.workspace(alpha) / "events.txt"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("1\n")
    await _create(container, alpha, "tail -n +1 -F events.txt", debounce_seconds=0.1)

    async with _running(_runner(container)):
        await _until(lambda: _has_sessions(container, alpha, 1))
        with events.open("a") as f:
            f.write("2\n3\n")
        # 第一次触发的会话还没运行（排着）：新事件排队，不开第二个会话
        await anyio.sleep(0.6)
        assert len(await _sessions(container, alpha)) == 1
        await drain(container)
        await _until(lambda: _has_sessions(container, alpha, 2))

    # 第二次触发可能落在 drain 还在跑的时候，那时信已被运行取走、在历史里
    latest = (await container.triggers.get_by_name("feed")).last_session_id
    async with container.db.transaction() as s:
        queued = [m.content for m in await queued_in(s, latest)]
    prompts = queued or [
        text
        for m in await history_of(container, latest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
        for text in ([p.content] if isinstance(p.content, str) else p.content)
    ]
    assert "handle\n\n<event>\n2\n3\n</event>" in prompts, prompts


async def test_a_command_that_keeps_exiting_is_restarted_recorded_and_alerted_once(container, make_agent):
    alpha = (await make_agent()).id
    await _create(container, alpha, "echo boom >&2; exit 3")

    async with _running(_runner(container)):

        async def failed_often() -> bool:
            return container.triggers.status_of(await container.triggers.get_by_name("feed"))["failures"] >= 5

        await _until(failed_often)

    status = container.triggers.status_of(await container.triggers.get_by_name("feed"))
    assert "exited with 3" in status["error"] and "boom" in status["error"]
    log = container.agents.workspace(alpha) / "trigger-logs" / "feed.stderr.log"
    assert log.read_text().count("boom") >= 5
    await drain(container)
    notes = await container.notifications.list(10)
    assert len([n for n in notes if "feed" in n.title]) == 1


async def test_a_killed_command_is_restarted_and_disabling_kills_it(container, make_agent):
    alpha = (await make_agent()).id
    pid_file = container.agents.workspace(alpha) / "pid"
    trigger = await _create(container, alpha, "echo $$ > pid; exec sleep 60")

    async def pid_written() -> bool:
        return pid_file.exists() and pid_file.read_text().strip() != ""

    async with _running(_runner(container)):
        await _until(pid_written)
        first = int(pid_file.read_text())
        pid_file.unlink()
        os.kill(first, 9)
        await _until(pid_written)
        second = int(pid_file.read_text())
        assert second != first and _alive(second)

        await container.triggers.update(trigger.id, enabled=False)

        async def gone() -> bool:
            return not _alive(second)

        await _until(gone)
        status = container.triggers.status_of(await container.triggers.get_by_name("feed"))
        assert status["running_since"] is None


async def test_changing_the_config_restarts_the_command_with_the_new_one(container, make_agent):
    alpha = (await make_agent()).id
    out = container.agents.workspace(alpha) / "which"
    trigger = await _create(container, alpha, "echo old > which; exec sleep 60")

    async def says(word: str) -> Callable:
        async def check() -> bool:
            return out.exists() and out.read_text().strip() == word

        return check

    async with _running(_runner(container)):
        await _until(await says("old"))
        await container.triggers.update(trigger.id, config={"command": "echo new > which; exec sleep 60"})
        await _until(await says("new"))


async def test_stopping_the_runner_kills_the_command(container, make_agent):
    alpha = (await make_agent()).id
    pid_file = container.agents.workspace(alpha) / "pid"
    await _create(container, alpha, "echo $$ > pid; exec sleep 60")

    async with _running(_runner(container)):

        async def written() -> bool:
            return pid_file.exists() and pid_file.read_text().strip() != ""

        await _until(written)
        pid = int(pid_file.read_text())

    async def gone() -> bool:
        return not _alive(pid)

    await _until(gone)


async def test_secrets_are_injected_and_redacted_from_events(container, make_agent):
    alpha = (await make_agent()).id
    await container.vault.set(alpha, "TOKEN", "s3cr3t-value")
    await _create(container, alpha, 'echo "got $TOKEN"; sleep 30', secrets=["TOKEN"], debounce_seconds=0)

    async with _running(_runner(container)):
        await _until(lambda: _has_sessions(container, alpha, 1))

    assert await _contents(container, alpha) == ["handle\n\n<event>\ngot [secret:TOKEN]\n</event>"]


async def test_a_missing_secret_is_recorded_as_the_error(container, make_agent):
    alpha = (await make_agent()).id
    await _create(container, alpha, "sleep 30", secrets=["NOPE"])

    async with _running(_runner(container)):

        async def failed() -> bool:
            return container.triggers.status_of(await container.triggers.get_by_name("feed"))["failures"] >= 1

        await _until(failed)

    assert "NOPE" in container.triggers.status_of(await container.triggers.get_by_name("feed"))["error"]


async def test_a_backlog_over_the_event_limit_fires_in_batches_without_waiting(container, make_agent):
    alpha = (await make_agent()).id
    await _create(
        container, alpha, "for i in $(seq 1 40); do echo line-$i; done; sleep 30", debounce_seconds=0.2
    )

    async with _running(_runner(container, max_bytes=100)):
        await _until(lambda: _has_sessions(container, alpha, 3))

    events = [c.split("<event>\n")[1].split("\n</event>")[0] for c in await _contents(container, alpha)]
    assert all(len(e.encode()) < 100 for e in events)
    assert any(e.startswith("line-1\n") for e in events)
