"""agent 之间的委派：delegate_task（异步，不等结果）、ask_agent（同步，内联跑完）、list_threads。设计见 runtime/conversation.py。"""

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, ModelRetry, RunContext, ToolReturn

from muliao.agents import AgentService
from muliao.conventions import TOOL_THREADS
from muliao.db import Database
from muliao.models import ReplyTo
from muliao.runtime.answers import Answer
from muliao.runtime.conversation import ReportTo, Threads, thread_name, threads_of, validate_thread
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.transcript import truncate

_ANSWER_PREVIEW = 500


class AgentInfo(BaseModel):
    name: str
    description: str


class ThreadInfo(BaseModel):
    thread: str
    agent: str
    # true：ask_agent 开出的、只有你能继续的 thread；false：用户也能看到并回复
    private: bool
    # 最近一轮的状态。私有 thread 只报告已完成的轮次（done，或还没有完成过任何一轮时为 idle）
    status: Literal["done", "failed", "running", "idle"]
    # 最近一轮已完成时的回答摘录
    answer: str | None


# 在私有 thread 里内联跑完一轮并返回回答（runtime.turn.TurnRunner.run_inline）：(session_id, prompt) -> answer
RunInline = Callable[[str, str], Awaitable[Answer]]
# 停止一个会话正在运行的一轮（runtime.turn.TurnRunner.stop）：session_id -> 是否有在运行的
StopTurn = Callable[[str], bool]


def delegation_toolset(
    db: Database, agents: AgentService, threads: Threads, run_inline: RunInline, stop_turn: StopTurn
) -> FunctionToolset[RunScope]:
    """委派：thread 是这个会话开出的、以名字区分的子会话，同名即续聊（保留对方上下文）。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def list_agents() -> list[AgentInfo]:
        """List all agents and what each one is for. Use before delegating."""
        return [AgentInfo(name=a.name, description=a.description) for a in await agents.list()]

    @ts.tool
    async def delegate_task(
        ctx: RunContext[RunScope], agent: str, task: str, thread: str, report_back: bool = False
    ) -> ToolReturn:
        """Hand a task to an agent and keep going without waiting. It runs in a thread the user can see and
        reply in. By default the user is notified with its answer and you do NOT get it back. With
        `report_back: true` the answer (or its failure) comes back to you as an event in this conversation
        instead, with a check-in event every few minutes while it is still working, so you can keep going and
        act on the result when it arrives. If you need the answer before you can continue, use `ask_agent`.

        Args:
            agent: Target agent name from `list_agents`.
            task: Self-contained task description; the agent cannot see this conversation except through
                earlier messages in the same thread.
            thread: Short name of the thread, e.g. "weekly-report". Reusing a thread name continues that
                conversation with its full context; use a new name for an unrelated task.
            report_back: Get the thread's answer back as an event in this conversation.
        """
        parent_id = _require_session(ctx)
        report_to = None
        if report_back:
            if ctx.deps.reply_to not in (ReplyTo.USER, ReplyTo.NONE):
                # 私有 thread 的发问方在同步等它的回答，没有自己的队列轮次来处理交回的事件
                raise ModelRetry("report_back is not available in a private thread; use ask_agent instead")
            report_to = ReportTo(session_id=parent_id, reply_to=ctx.deps.reply_to)
        target = await agents.find_by_name(agent)
        async with db.transaction() as s:
            received = await threads.send(
                s, parent_id=parent_id, agent=target, thread=thread, prompt=task, report_to=report_to
            )
        return ToolReturn(
            return_value=f"delegated to {target.name} in thread {validate_thread(thread)!r}",
            metadata={TOOL_THREADS: [received.session.id]},
        )

    @ts.tool
    async def ask_agent(ctx: RunContext[RunScope], agent: str, message: str, thread: str) -> ToolReturn:
        """Ask an agent and wait for its answer, which is returned as this tool's result. The thread is private
        to this conversation (the user can read but not reply). Files it shares are shown to the user with your reply. Ask again in the same thread to follow up with
        the agent's context intact, e.g. review -> fix -> review again. Several asks in one response run in parallel.

        Args:
            agent: Target agent name from `list_agents`.
            message: Self-contained request; the agent sees only this thread's history.
            thread: Short name of the thread, e.g. "code-review". Reuse it to continue the same conversation.
        """
        parent_id = _require_session(ctx)
        target = await agents.find_by_name(agent)
        if not message.strip():
            raise ModelRetry("message must not be empty")
        async with db.transaction() as s:
            child = await threads.open(s, parent_id=parent_id, agent=target, thread=thread, private=True)
        # 在调用方这一轮里直接跑完：失败随调用方这一轮失败，由队列统一重试
        return (await run_inline(child.id, message)).to_tool_return(thread_id=child.id)

    @ts.tool
    async def stop_thread(ctx: RunContext[RunScope], thread: str) -> str:
        """Stop the task a thread of this conversation is running right now (what it already did is kept).
        With `report_back` you then get an event saying it was stopped.

        Args:
            thread: The thread name used with `delegate_task`.
        """
        parent_id = _require_session(ctx)
        async with db.transaction() as s:
            child = await threads.find(s, parent_id, validate_thread(thread))
        if child is None:
            raise ModelRetry(f"this conversation has no thread named {thread!r}; see list_threads")
        if not stop_turn(child.id):
            raise ModelRetry(f"thread {thread!r} is not running anything right now")
        return f"stopping thread {thread!r}"

    @ts.tool
    async def list_threads(ctx: RunContext[RunScope]) -> list[ThreadInfo]:
        """List the threads this conversation has opened, with the state of each one's latest task."""
        parent_id = _require_session(ctx)
        names = {a.id: a.name for a in await agents.list()}
        async with db.transaction() as s:
            result = []
            for child in await threads_of(s, parent_id):
                state = await threads.state(s, child)
                result.append(
                    ThreadInfo(
                        thread=thread_name(child),
                        agent=names[child.agent_id],
                        private=child.private,
                        status=state.status,
                        answer=truncate(state.text, _ANSWER_PREVIEW) if state.status == "done" else None,
                    )
                )
        return result

    return ts


def _require_session(ctx: RunContext[RunScope]) -> str:
    if ctx.deps.session_id is None:
        raise ModelRetry("delegation is only available inside a conversation")
    return ctx.deps.session_id
