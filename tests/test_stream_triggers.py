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


async def test_a_backlog_over_the_event_limit_waits_in_the_pipe_and_goes_one_session_at_a_time(
    container, make_agent
):
    """排队以事件上限为界：排满后不再读命令的输出（命令阻塞），不重叠、不丢。"""
    alpha = (await make_agent()).id
    await _create(
        container, alpha, "for i in $(seq 1 40); do echo line-$i; done; sleep 30", debounce_seconds=0
    )

    async with _running(_runner(container, max_bytes=100)):
        await _until(lambda: _has_sessions(container, alpha, 1))
        await anyio.sleep(0.3)
        # 第一次触发的会话还没做完：剩下的行留着，不另开会话
        assert len(await _sessions(container, alpha)) == 1
        while len(await _sessions(container, alpha)) < 4:
            await drain(container)

    events = []
    for item in await _sessions(container, alpha):
        session = item.session.id
        async with container.db.transaction() as s:
            queued = [m.content for m in await queued_in(s, session)]
        events += [p for p in queued + await _prompts(container, session) if "<event>" in p]
    lines = [line for e in events for line in e.split("<event>\n")[1].split("\n</event>")[0].split("\n")]
    assert all(len(e.split("<event>\n")[1].encode()) < 100 + len("</event>") for e in events)
    assert sorted(lines, key=lambda x: int(x.split("-")[1])) == [
        f"line-{i}" for i in range(1, len(lines) + 1)
    ]


async def _prompts(container, session_id: str) -> list[str]:
    return [
        text
        for m in await history_of(container, session_id)
        for p in m.parts
        if isinstance(p, UserPromptPart)
        for text in ([p.content] if isinstance(p.content, str) else p.content)
        if isinstance(text, str)
    ]


async def test_an_overlong_line_is_dropped_and_the_following_lines_still_arrive(container, make_agent):
    alpha = (await make_agent()).id
    await _create(container, alpha, "printf '%0300d\\nok\\n' 0; sleep 30", debounce_seconds=0.2)

    async with _running(_runner(container, max_bytes=100)):
        await _until(lambda: _has_sessions(container, alpha, 1))

    assert await _contents(container, alpha) == ["handle\n\n<event>\nok\n</event>"]


async def test_saving_the_same_config_keeps_the_running_state(container, make_agent):
    """只改说明（界面保存时总会带上原配置）不重启进程，运行状态也不能被清掉。"""
    alpha = (await make_agent()).id
    trigger = await _create(container, alpha, "sleep 30")

    async def running() -> bool:
        return (
            container.triggers.status_of(await container.triggers.get_by_name("feed"))["running_since"]
            is not None
        )

    async with _running(_runner(container)):
        await _until(running)
        await container.triggers.update(trigger.id, prompt="new", config={"command": "sleep 30"})
        await anyio.sleep(0.3)
        assert await running()


async def test_an_exit_is_noticed_even_when_a_child_keeps_the_pipes_and_the_child_is_killed(
    container, make_agent
):
    alpha = (await make_agent()).id
    kids = container.agents.workspace(alpha) / "kids"
    await _create(container, alpha, "sleep 30 & echo $! >> kids; exit 1")

    async with _running(_runner(container)):

        async def failed_twice() -> bool:
            return container.triggers.status_of(await container.triggers.get_by_name("feed"))["failures"] >= 2

        await _until(failed_twice)

    first = int(kids.read_text().split()[0])

    async def gone() -> bool:
        return not _alive(first)

    await _until(gone)


async def test_running_stably_clears_the_error(container, make_agent):
    alpha = (await make_agent()).id
    await _create(
        container, alpha, "if [ -f once ]; then sleep 30; else touch once; echo bad >&2; exit 1; fi"
    )

    async with _running(_runner(container, stable_seconds=0.3)):

        async def recovered() -> bool:
            status = container.triggers.status_of(await container.triggers.get_by_name("feed"))
            return status["running_since"] is not None and status["error"] is None and status["failures"] == 0

        async def failed() -> bool:
            return container.triggers.status_of(await container.triggers.get_by_name("feed"))["failures"] == 1

        await _until(failed)
        await _until(recovered)


async def test_stderr_is_logged_with_secrets_redacted(container, make_agent):
    alpha = (await make_agent()).id
    await container.vault.set(alpha, "TOKEN", "s3cr3t-value")
    await _create(container, alpha, 'echo "err $TOKEN" >&2; sleep 30', secrets=["TOKEN"])
    log = container.agents.workspace(alpha) / "trigger-logs" / "feed.stderr.log"

    async def logged() -> bool:
        return log.exists() and "err " in log.read_text()

    async with _running(_runner(container)):
        await _until(logged)

    assert "err [secret:TOKEN]" in log.read_text() and "s3cr3t" not in log.read_text()


async def test_a_waiting_previous_session_holds_events_and_notifies_once(container, make_agent):
    from muliao.sessions.holds import get_in, hold_in, release

    alpha = (await make_agent()).id
    events = container.agents.workspace(alpha) / "events.txt"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("1\n")
    await _create(container, alpha, "tail -n +1 -F events.txt", debounce_seconds=0)

    async with _running(_runner(container)):
        await _until(lambda: _has_sessions(container, alpha, 1))
        await drain(container)
        first = (await container.triggers.get_by_name("feed")).last_session_id
        async with container.db.transaction() as s:
            await hold_in(s, first, "job:1", awaited=False)
        for line in ("2\n", "3\n"):
            with events.open("a") as f:
                f.write(line)
            await anyio.sleep(0.3)
        await drain(container)
        notes = [n for n in await container.notifications.list(10) if "feed" in n.title]
        assert [n.session_id for n in notes] == [first]
        assert len(await _sessions(container, alpha)) == 1

        async with container.db.transaction() as s:
            hold = await get_in(s, first, "job:1")
            assert hold is not None
            release(hold)
        await _until(lambda: _has_sessions(container, alpha, 2))


async def test_an_unexpected_failure_of_one_stream_is_a_restart_and_leaves_the_others_running(
    container, make_agent, monkeypatch
):
    """监督者的异常边界：一个触发器启动时意外失败（如日志写不进去）按一次失败退避重启，别的触发器与对账循环照常。"""
    from muliao.triggers import stream as stream_module

    alpha = (await make_agent()).id
    real = stream_module._prepare_log
    broken = {"count": 0}

    def flaky(path, header):  # noqa: ANN001, ANN202
        if path.name.startswith("bad.") and broken["count"] < 2:
            broken["count"] += 1
            raise OSError("disk full")
        real(path, header)

    monkeypatch.setattr(stream_module, "_prepare_log", flaky)
    await container.triggers.create(
        name="bad",
        agent_id=alpha,
        kind="stream",
        prompt="p",
        config={"command": "echo $$ > bad.pid; sleep 30"},
    )
    await _create(container, alpha, "echo good; sleep 30", debounce_seconds=0)

    async with _running(_runner(container)):
        await _until(lambda: _has_sessions(container, alpha, 1))
        pid_file = container.agents.workspace(alpha) / "bad.pid"

        async def restarted() -> bool:
            return pid_file.exists() and pid_file.read_text().strip() != ""

        await _until(restarted)

    status = container.triggers.status_of(await container.triggers.get_by_name("bad"))
    assert status["failures"] == 2 and "disk full" in status["error"]
    assert await _contents(container, alpha) == ["handle\n\n<event>\ngood\n</event>"]


async def test_events_survive_a_failure_while_checking_the_previous_session(container, make_agent):
    alpha = (await make_agent()).id
    await _create(container, alpha, "echo one; sleep 30", debounce_seconds=0)
    runner = _runner(container)
    real = runner._wait_previous
    calls = {"count": 0}

    async def flaky(*args):  # noqa: ANN002, ANN202
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("database is locked")
        await real(*args)

    runner._wait_previous = flaky  # type: ignore[method-assign]

    async with _running(runner):
        await _until(lambda: _has_sessions(container, alpha, 1))

    assert calls["count"] == 2
    assert await _contents(container, alpha) == ["handle\n\n<event>\none\n</event>"]
