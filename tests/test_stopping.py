"""三种取消各自的语义（runtime.stopping，docs/domain-design.md「并发模型」）：用户停止只停这次运行并返回 Stopped；
关停与总时限的取消照常向外抛出；无论哪种，运行里工具的子任务（pydantic-ai 的工具在原生任务里执行）都如实收到取消，
不会被工具内部的 anyio 作用域吞掉、当成正常结束。"""

import asyncio

import anyio
import pytest

from muliao.runtime.stopping import Stopped, StoppableRuns


class Tool:
    """像 pydantic-ai 那样把工具放进原生任务并 await 它；工具内部用 anyio 的任务组与时限（同 runtime.shell）。"""

    def __init__(self) -> None:
        self.outcome: str | None = None
        self.started = anyio.Event()

    async def _body(self) -> None:
        try:
            with anyio.fail_after(120):
                async with anyio.create_task_group() as group:
                    group.start_soon(anyio.sleep, 60)
                    self.started.set()
            self.outcome = "returned"
        except TimeoutError:
            self.outcome = "timeout"
            raise
        except asyncio.CancelledError:
            self.outcome = "cancelled"
            raise

    async def run(self) -> str:
        task = asyncio.create_task(self._body())
        await task
        return "done"


async def test_a_user_stop_returns_stopped_and_cancels_the_tool_for_real():
    runs: StoppableRuns[str] = StoppableRuns()
    tool = Tool()

    async def stop_when_started() -> None:
        await tool.started.wait()
        assert runs.stop("s")

    async with anyio.create_task_group() as group:
        group.start_soon(stop_when_started)
        result = await runs.run("s", tool.run)

    assert isinstance(result, Stopped)
    assert tool.outcome == "cancelled"
    assert not runs.stop("s")


async def test_a_shutdown_propagates_out_of_the_run_and_cancels_the_tool():
    runs: StoppableRuns[str] = StoppableRuns()
    tool = Tool()
    results: list[object] = []

    async def run() -> None:
        results.append(await runs.run("s", tool.run))

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await tool.started.wait()
        group.cancel_scope.cancel()

    assert results == [] and tool.outcome == "cancelled"


async def test_a_task_timeout_raises_timeout_and_cancels_the_tool():
    runs: StoppableRuns[str] = StoppableRuns()
    tool = Tool()

    with pytest.raises(TimeoutError), anyio.fail_after(0.3):
        await runs.run("s", tool.run)

    assert tool.outcome == "cancelled"


async def test_two_runs_of_one_session_at_once_is_a_bug():
    runs: StoppableRuns[str] = StoppableRuns()
    tool = Tool()

    async with anyio.create_task_group() as group:
        group.start_soon(runs.run, "s", tool.run)
        await tool.started.wait()
        with pytest.raises(RuntimeError, match="already has a running run"):
            await runs.run("s", tool.run)
        runs.stop("s")
