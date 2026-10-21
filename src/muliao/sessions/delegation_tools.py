"""agent 之间的委派：delegate_task（不等结果）、ask_agent（挂起这次调用，等对方的结局作为结果）、stop_thread、
list_threads。委派的生命周期见 sessions.delegation。"""

from collections.abc import Awaitable, Callable

from pydantic import BaseModel
from pydantic_ai import CallDeferred, FunctionToolset, ModelRetry, RunContext, ToolReturn

from muliao.agent_runtime.api import TOOL_THREADS
from muliao.agents.api import AgentService
from muliao.platform.db import Database
from muliao.platform.text_edit import truncate
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.sessions.activity import WorkState
from muliao.sessions.kinds import SessionKind, policy_of
from muliao.sessions.threads import Threads, validate_thread

_ANSWER_PREVIEW = 500


class AgentInfo(BaseModel):
    name: str
    description: str


class ThreadInfo(BaseModel):
    thread: str
    agent: str
    # true：ask_agent 开出的、只有你能继续的 thread；false：用户也能看到并回复
    private: bool
    # running：在处理；waiting：在等它自己开出的后台任务、委派或提问的回答，还没做完；idle：没有进行中的事
    status: WorkState
    # 最近一次运行最终失败时的错误说明
    failure: str | None
    # 它最近的回答摘录（还没有回答时为 None）；status 不是 idle 时是中间的回答，不是结局
    answer: str | None


# 停止一个会话：停下它的消费、清空信箱（sessions.consumer.Consumer.stop_session）
StopSession = Callable[[str], Awaitable[None]]


_DELEGATE_DESCRIPTION = """\
Hand a task to an agent and keep going without waiting. It runs in a thread the user can see and reply in. \
By default the user is notified with its answer and you do NOT get it back. With `report_back: true` the answer \
(or its failure) comes back to you as an event in this conversation instead, with a check-in event about every \
{check:g} seconds while it is still working (events reach you like messages, between your tool calls if you are \
busy), so you can keep going and act on the result when it arrives. If you need the answer before you can continue, use `ask_agent`."""


def delegation_toolset(
    db: Database,
    agents: AgentService,
    threads: Threads,
    stop_session: StopSession,
    check_seconds: float,
) -> FunctionToolset[RunScope]:
    """委派：thread 是这个会话开出的、以名字区分的子会话，同名即续聊（保留对方上下文）。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def list_agents() -> list[AgentInfo]:
        """List all agents and what each one is for. Use before delegating."""
        return [AgentInfo(name=a.name, description=a.description) for a in await agents.list()]

    # 检查间隔是配置的值（runtime.job_check_seconds），写进说明
    @ts.tool(description=_DELEGATE_DESCRIPTION.format(check=check_seconds))
    async def delegate_task(
        ctx: RunContext[RunScope], agent: str, task: str, thread: str, report_back: bool = False
    ) -> ToolReturn:
        """The description is `_DELEGATE_DESCRIPTION` (it carries the configured check-in interval).

        Args:
            agent: Target agent name from `list_agents`.
            task: Self-contained task description; the agent cannot see this conversation except through
                earlier messages in the same thread.
            thread: Short name of the thread, e.g. "weekly-report". Reusing a thread name continues that
                conversation with its full context; use a new name for an unrelated task.
            report_back: Get the thread's answer back as an event in this conversation.
        """
        parent_id = _require_session(ctx)
        target = await agents.find_by_name(agent)
        async with db.transaction() as s:
            if report_back:
                if ctx.tool_call_id is None:
                    raise RuntimeError("delegate_task was called without a tool call id")
                # 续跑重新执行这次调用时按调用 id 去重
                child = await threads.dispatch(
                    s,
                    owner_id=parent_id,
                    agent=target,
                    thread=thread,
                    prompt=task,
                    kind=SessionKind.THREAD,
                    call_id=None,
                    awaited=ctx.deps.user_waits,
                    key=ctx.tool_call_id,
                )
            else:
                child = (
                    await threads.send(s, parent_id=parent_id, agent=target, thread=thread, prompt=task)
                ).session
        return ToolReturn(
            return_value=f"delegated to {target.name} in thread {validate_thread(thread)!r}",
            metadata={TOOL_THREADS: [child.id]},
        )

    @ts.tool
    async def ask_agent(ctx: RunContext[RunScope], agent: str, message: str, thread: str) -> str:
        """Ask an agent and wait for its answer, which is returned as this tool's result. The thread is private
        to this conversation (the user can read but not reply). Files it shares are shown to the user with your
        reply. Ask again in the same thread to follow up with the agent's context intact, e.g. review -> fix ->
        review again. Several asks in one response run in parallel. Your reply pauses until the answers arrive; if
        the user writes to you meanwhile, you continue and the answer reaches you later as an event.

        Args:
            agent: Target agent name from `list_agents`.
            message: Self-contained request; the agent sees only this thread's history.
            thread: Short name of the thread, e.g. "code-review". Reuse it to continue the same conversation.
        """
        parent_id = _require_session(ctx)
        target = await agents.find_by_name(agent)
        if not message.strip():
            raise ModelRetry("message must not be empty")
        if ctx.tool_call_id is None:
            raise RuntimeError("ask_agent was called without a tool call id")
        async with db.transaction() as s:
            # 续跑时重新执行这次调用：已经投过就只再挂起（Threads.dispatch 按调用 id 幂等）
            await threads.dispatch(
                s,
                owner_id=parent_id,
                agent=target,
                thread=thread,
                prompt=message,
                kind=SessionKind.PRIVATE_THREAD,
                call_id=ctx.tool_call_id,
                awaited=ctx.deps.user_waits,
                key=ctx.tool_call_id,
            )
        # 发问方的运行停在这次调用上，不占 worker；对方的结局作为这次调用的结果交回（sessions.pending）
        raise CallDeferred()

    @ts.tool
    async def stop_thread(ctx: RunContext[RunScope], thread: str) -> str:
        """Stop a thread of this conversation, whether it is running or still queued: its current run stops (what it
        already did is kept) and the work queued for it is dropped. If you are waiting for its answer (`ask_agent`,
        or `report_back`), you get its outcome saying it was stopped.

        Args:
            thread: The thread name used with `delegate_task` or `ask_agent`.
        """
        parent_id = _require_session(ctx)
        async with db.transaction() as s:
            child_id = await threads.thread_id_in(s, parent_id, validate_thread(thread))
        if child_id is None:
            raise ModelRetry(f"this conversation has no thread named {thread!r}; see list_threads")
        await stop_session(child_id)
        return f"stopped thread {thread!r}"

    @ts.tool
    async def list_threads(ctx: RunContext[RunScope]) -> list[ThreadInfo]:
        """List the threads this conversation has opened. `status`: running (working now), waiting (its run ended \
but it is waiting for work it started or answers it asked for: not finished yet), idle (nothing in progress). \
`answer` is its latest reply; while it is not idle that reply is intermediate, not its result. `failure` is set \
when its latest run failed for good."""
        parent_id = _require_session(ctx)
        async with db.transaction() as s:
            views = await threads.list_in(s, parent_id)
        # agents.get 也取得到正在删除的 agent（它的 thread 在删除流程里随后删除）
        names = {agent_id: (await agents.get(agent_id)).name for agent_id in {v.agent_id for v in views}}
        return [
            ThreadInfo(
                thread=v.thread,
                agent=names[v.agent_id],
                private=not policy_of(v.kind).user_writable,
                status=v.state.work,
                failure=v.state.failure,
                answer=truncate(v.state.answer, _ANSWER_PREVIEW) if v.state.answer else None,
            )
            for v in views
        ]

    return ts


def _require_session(ctx: RunContext[RunScope]) -> str:
    """委派组只组合进有会话的运行（反思与临时 agent 的工具组合里没有它）。"""
    if ctx.deps.session_id is None:
        raise RuntimeError(f"delegation tools used outside a session by agent {ctx.deps.agent_id}")
    return ctx.deps.session_id
