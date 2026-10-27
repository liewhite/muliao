"""三种取消各自的语义（sessions.stopping，docs/architecture.md「并发模型」）：用户停止只停这次运行并返回 Stopped；
关停等外层取消照常向外抛出；无论哪种，运行里工具的子任务（pydantic-ai 的工具在原生任务里执行）都如实收到取消，
不会被工具内部的 anyio 作用域吞掉、当成正常结束。"""

import asyncio
from contextlib import asynccontextmanager

import anyio
import anyio.lowlevel
import pytest

from muliao.sessions.stopping import StoppableRuns, Stopped


@asynccontextmanager
async def _no_gate():
    yield


class Tool:
    """像 pydantic-ai 那样把工具放进原生任务并 await 它；工具内部用 anyio 的任务组与时限（同 platform.shell）。"""

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
    runs: StoppableRuns[str] = StoppableRuns(_no_gate)
    tool = Tool()

    async def stop_when_started() -> None:
        await tool.started.wait()
        assert await runs.stop("s")

    async with anyio.create_task_group() as group:
        group.start_soon(stop_when_started)
        result = await runs.run("s", tool.run)

    assert isinstance(result, Stopped)
    assert tool.outcome == "cancelled"
    assert not await runs.stop("s")


async def test_a_shutdown_propagates_out_of_the_run_and_cancels_the_tool():
    runs: StoppableRuns[str] = StoppableRuns(_no_gate)
    tool = Tool()
    results: list[object] = []

    async def run() -> None:
        results.append(await runs.run("s", tool.run))

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await tool.started.wait()
        group.cancel_scope.cancel()

    assert results == [] and tool.outcome == "cancelled"
    # 被外层取消的运行也撤销了登记：之后的停止不会落到它身上
    assert not await runs.stop("s")


async def test_an_outer_timeout_raises_timeout_and_cancels_the_tool():
    runs: StoppableRuns[str] = StoppableRuns(_no_gate)
    tool = Tool()

    with pytest.raises(TimeoutError), anyio.fail_after(0.3):
        await runs.run("s", tool.run)

    assert tool.outcome == "cancelled"


async def test_two_runs_of_one_session_at_once_is_a_bug():
    runs: StoppableRuns[str] = StoppableRuns(_no_gate)
    tool = Tool()

    async with anyio.create_task_group() as group:
        group.start_soon(runs.run, "s", tool.run)
        await tool.started.wait()
        with pytest.raises(RuntimeError, match="already has a running run"):
            await runs.run("s", tool.run)
        await runs.stop("s")


class CleaningTool:
    """被取消后还要做完一段屏蔽着的清理（同 jobs.start 删掉没起来的行、resources 等 git 退出）。"""

    def __init__(self, cleanup_seconds: float = 0.2) -> None:
        self.cleaned = False
        self.started = anyio.Event()
        self.cleaning = anyio.Event()
        self._cleanup_seconds = cleanup_seconds

    async def _body(self) -> None:
        try:
            self.started.set()
            await anyio.sleep(60)
        except asyncio.CancelledError:
            self.cleaning.set()
            with anyio.CancelScope(shield=True):
                await anyio.sleep(self._cleanup_seconds)
                self.cleaned = True
            raise

    async def run(self) -> str:
        await asyncio.create_task(self._body())
        return "done"


async def test_stopping_twice_does_not_interrupt_the_runs_shielded_cleanup():
    runs: StoppableRuns[str] = StoppableRuns(_no_gate)
    tool = CleaningTool()

    async def stop_twice() -> None:
        await tool.started.wait()
        await runs.stop("s")
        await tool.cleaning.wait()
        assert await runs.stop("s")

    async with anyio.create_task_group() as group:
        group.start_soon(stop_twice)
        result = await runs.run("s", tool.run)

    assert isinstance(result, Stopped) and tool.cleaned


async def test_a_shutdown_during_a_stopped_runs_cleanup_does_not_interrupt_it():
    runs: StoppableRuns[str] = StoppableRuns(_no_gate)
    tool = CleaningTool()

    async with anyio.create_task_group() as group:
        group.start_soon(runs.run, "s", tool.run)
        await tool.started.wait()
        await runs.stop("s")
        await tool.cleaning.wait()
        group.cancel_scope.cancel()

    assert tool.cleaned


async def test_a_run_that_swallows_its_cancellation_stays_registered(monkeypatch):
    monkeypatch.setattr("muliao.sessions.stopping._WIND_DOWN_SECONDS", 0.1)
    runs: StoppableRuns[str] = StoppableRuns(_no_gate)
    tool = CleaningTool(cleanup_seconds=1.0)

    with pytest.raises(TimeoutError), anyio.fail_after(0.2):
        await runs.run("s", tool.run)

    # 收尾还没结束：同一会话的下一次运行报错，不会和它同时操作会话
    with pytest.raises(RuntimeError, match="already has a running run"):
        await runs.run("s", tool.run)


async def _write_forever(container, counter: list[int]) -> None:
    """不停地开事务写库：取消迟早会落在某个事务进行中的时刻。"""
    from muliao.agents.tables import Agent

    while True:
        async with container.db.transaction() as s:
            agent = await s.get(Agent, "probe")
            agent.description = str(counter[0])
            await s.flush()
        counter[0] += 1
        # 真实的调用方在两个事务之间总有别的等待（模型、工具）
        await anyio.lowlevel.checkpoint()


async def _leftover_tasks() -> list[str]:
    return [repr(t.get_coro()) for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def test_an_anyio_cancellation_waits_for_the_transaction_and_leaves_nothing_behind(
    container, make_agent
):
    from muliao.agents.tables import Agent, AgentRulesRow, Memory

    agent = await make_agent("probe")
    async with container.db.transaction() as s:
        # _write_forever 按固定的 id 写：先挪开引用它的记忆与规则行
        await s.execute(Memory.__table__.delete().where(Memory.agent_id == agent.id))
        await s.execute(AgentRulesRow.__table__.delete().where(AgentRulesRow.agent_id == agent.id))
        await s.execute(Agent.__table__.update().where(Agent.id == agent.id).values(id="probe"))
    for _ in range(20):
        counter = [0]
        with anyio.move_on_after(0.01):
            await _write_forever(container, counter)
    await container.db.engine.dispose()
    assert not any("terminate" in t for t in await _leftover_tasks())


async def test_a_user_stop_never_cancels_a_statement_in_flight(tmp_path, caplog):
    """原生取消（用户停止）只在没有事务进行时发出（db.Database.exclusive）：语句不会被打断，连接不会被作废，关停时
    没有等不到的关闭任务。"""
    from sqlalchemy import text

    from muliao.platform.db import Database, create_engine

    db = Database(create_engine(tmp_path / "x.db"))
    async with db.transaction() as s:
        await s.execute(text("create table t(x int)"))

    async def write_back_to_back() -> None:
        while True:
            async with db.transaction() as s:
                await s.execute(text("insert into t values (1)"))
                await s.execute(text("select count(*) from t"))

    runs: StoppableRuns[None] = StoppableRuns(db.exclusive)
    for k in range(30):

        async def stop_soon(delay: float = 0.01 + k * 0.001) -> None:
            await anyio.sleep(delay)
            await runs.stop("s")

        async with anyio.create_task_group() as group:
            group.start_soon(stop_soon)
            assert isinstance(await runs.run("s", write_back_to_back), Stopped)

    async with db.transaction() as s:
        assert (await s.execute(text("select count(*) from t"))).scalar() > 0
    await db.engine.dispose()
    # 语句被打断时 SQLAlchemy 作废连接并记下 "Exception terminating connection"
    assert "terminating connection" not in caplog.text
    assert not any("terminate" in t for t in await _leftover_tasks())
