"""agent 之间的委派：delegate_task（异步）、ask_agent（同步）、wait_for、list_threads。设计见 runtime/conversation.py。"""

from typing import Literal

from pydantic import BaseModel
from pydantic_ai import CallDeferred, FunctionToolset, ModelRetry, RunContext

from muliao.agents import AgentService
from muliao.db import Database
from muliao.models import ReplyTo
from muliao.runtime.conversation import Threads, thread_name, threads_of, validate_thread
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.transcript import truncate

_ANSWER_PREVIEW = 500


class ThreadInfo(BaseModel):
    thread: str
    agent: str
    # true：ask_agent 开出的、只有你能继续的 thread；false：用户也能看到并回复
    private: bool
    status: Literal["done", "failed", "running", "idle"]
    # 最近一轮已完成时的回答摘录；完整回答用 wait_for(thread)
    answer: str | None


def delegation_toolset(db: Database, agents: AgentService, threads: Threads) -> FunctionToolset[RunScope]:
    """委派：thread 是这个会话开出的、以名字区分的子会话，同名即续聊（保留对方上下文）。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def delegate_task(ctx: RunContext[RunScope], agent: str, task: str, thread: str) -> str:
        """Hand a task to an agent and keep going without waiting. It runs in a thread the user can see and
        reply in; the user is notified with its answer. Call `wait_for(thread)` if you need the answer yourself
        before you finish; if you end your turn instead, the answer is NOT sent back to you.

        Args:
            agent: Target agent name from `list_agents`.
            task: Self-contained task description; the agent cannot see this conversation except through
                earlier messages in the same thread.
            thread: Short name of the thread, e.g. "weekly-report". Reusing a thread name continues that
                conversation with its full context; use a new name for an unrelated task.
        """
        parent_id = _require_session(ctx)
        target = await agents.find_by_name(agent)
        async with db.transaction() as s:
            await threads.send(
                s,
                parent_id=parent_id,
                agent=target,
                thread=thread,
                prompt=task,
                private=False,
                reply_to=ReplyTo.USER,
            )
        thread = validate_thread(thread)
        return f"delegated to {target.name} in thread {thread!r}; call wait_for({thread!r}) if you need the answer"

    @ts.tool
    async def ask_agent(ctx: RunContext[RunScope], agent: str, message: str, thread: str) -> str:
        """Ask an agent and wait for its answer, which is returned as this tool's result. The thread is private
        to this conversation (the user can read but not reply). Ask again in the same thread to follow up with
        the agent's context intact, e.g. review -> fix -> review again. Several asks in one response run in parallel.

        Args:
            agent: Target agent name from `list_agents`.
            message: Self-contained request; the agent sees only this thread's history.
            thread: Short name of the thread, e.g. "code-review". Reuse it to continue the same conversation.
        """
        parent_id = _require_session(ctx)
        target = await agents.find_by_name(agent)
        thread = validate_thread(thread)
        if not message.strip():
            raise ModelRetry("message must not be empty")
        # 现在就占用 thread：同一响应里并行的调用若争用同名 thread，冲突在这里作为可纠正的错误交回模型，
        # 而不是到提交时才让整轮失败
        async with db.transaction() as s:
            await threads.open(s, parent_id=parent_id, agent=target, thread=thread, private=True)
        raise CallDeferred(
            metadata={"action": "ask", "agent_id": target.id, "thread": thread, "message": message}
        )

    @ts.tool
    async def wait_for(ctx: RunContext[RunScope], thread: str) -> str:
        """Wait for the latest task in a thread of this conversation to finish and return its answer
        (prefixed with "[failed]" if it failed). Returns at once if it has already finished.

        Args:
            thread: Thread name used with `delegate_task` or `ask_agent`.
        """
        parent_id = _require_session(ctx)
        async with db.transaction() as s:
            child = await threads.find(s, parent_id, thread)
            if child is None:
                raise ModelRetry(f"this conversation has no thread named {thread!r}")
            state = await threads.state(s, child)
        match state.status:
            case "done":
                return state.text
            case "failed":
                return f"[failed] {state.text}"
            case "running":
                raise CallDeferred(metadata={"action": "wait", "turn_id": state.turn_id})
            case "idle":
                raise ModelRetry(f"thread {thread!r} has no task to wait for")

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
