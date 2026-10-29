"""agent 之间的委派：delegate_task（不等结果）、ask_agent（挂起这次调用，等对方的结局作为结果）、stop_thread、
list_threads。委派的生命周期见 sessions.delegation。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_runtime.api import defer_call
from muliao.agents.api import AgentService
from muliao.errors import InvalidInput, NotFound
from muliao.platform.db import Database
from muliao.platform.text_edit import truncate
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.sessions.activity import WorkState
from muliao.sessions.deferred import ASK_AGENT
from muliao.sessions.halt import Halted, StopSession
from muliao.sessions.threads import Threads, validate_thread
from muliao.sessions.work import DEFAULT_CHECK_SECONDS

_ANSWER_PREVIEW = 500


class ThreadInfo(BaseModel):
    thread: str
    agent: str
    # running：在处理；waiting：在等它自己开出的后台任务、委派、到点唤醒或提问的回答，还没做完；idle：没有进行中的事
    state: WorkState
    # 最近一次运行最终失败时的错误说明
    failure: str | None
    # 它最近的回答摘录（还没有回答时为 None）；state 不是 idle 时是中间的回答，不是结局
    answer: str | None


def delegation_toolset(
    db: Database,
    agents: AgentService,
    threads: Threads,
    stop_session: StopSession,
) -> FunctionToolset[RunScope]:
    """委派：thread 是这个会话开出的、以（agent, 名字）区分的子会话，同名即续聊（保留对方上下文），用 delegate_task
    还是 ask_agent 续聊都行。

    ask_agent 是阻塞的（调用不返回、一直等到结局），不设超时：agent 的运行本身有界；delegate_task(report_back)
    与发起方并发，按 heartbeat_seconds 投运行中检查（sessions.delegation.ThreadChecks）。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def delegate_task(
        ctx: RunContext[RunScope],
        agent: str,
        task: str,
        thread: str,
        report_back: bool = False,
        heartbeat_seconds: float | None = None,
    ) -> str:
        """Hand a task to an agent and keep going without waiting (non-blocking, no timeout). It runs in a thread
        the user can see and reply in. By default the user is notified with its answer and you do NOT get it back.
        With `report_back: true` the answer (or its failure) comes back to you as an event in this conversation
        instead (events reach you like messages, between your tool calls if you are busy), and while it is still
        working you get a check-in event every `heartbeat_seconds` (default 60) saying what the thread is doing:
        then either keep waiting (if nothing needs doing, reply with just '…'; `list_threads` shows its latest
        reply) or stop it with `stop_thread`. If you need the answer before you can continue, use `ask_agent`.
        The agent can be yourself (your own name): a fresh conversation of you, with your memory, skills and
        tools, for work to run on the side.

        Args:
            agent: Target agent name from `list_agents`, or your own name.
            task: Self-contained task description; the agent cannot see this conversation except through
                earlier messages in the same thread.
            thread: Short name of the thread, e.g. "weekly-report". Reusing a thread name with the same agent
                continues that conversation with its full context, whether it was opened with `delegate_task` or
                `ask_agent`; use a new name for an unrelated task.
            report_back: Get the thread's answer back as an event in this conversation.
            heartbeat_seconds: With `report_back: true` only: seconds between check-in events while it works
                (> 0, default 60); choose it from how long you expect the task to take, e.g. 600 for an hour-long
                task.
        """
        parent_id = ctx.deps.session_id
        target = await agents.find_by_name(agent)
        if heartbeat_seconds is not None and not report_back:
            raise InvalidInput(
                "heartbeat_seconds needs `report_back: true`; without it the result goes to the user, not to you"
            )
        check = DEFAULT_CHECK_SECONDS if heartbeat_seconds is None else heartbeat_seconds
        if not check > 0:  # 也拒绝 NaN
            raise InvalidInput("heartbeat_seconds must be greater than 0")
        if ctx.tool_call_id is None:
            raise RuntimeError("delegate_task was called without a tool call id")
        async with db.transaction() as s:
            if report_back:
                # 续跑重新执行这次调用时按调用 id 去重
                await threads.dispatch(
                    s,
                    parent_id=parent_id,
                    agent=target,
                    thread=thread,
                    prompt=task,
                    call_id=ctx.tool_call_id,
                    suspends=False,
                    awaited=ctx.deps.user_waits,
                    check_seconds=check,
                )
            else:
                await threads.send(
                    s, parent_id=parent_id, agent=target, thread=thread, prompt=task, call_id=ctx.tool_call_id
                )
        return f"delegated to {target.name} in thread {validate_thread(thread)!r}"

    @ts.tool(name=ASK_AGENT)
    async def ask_agent(
        ctx: RunContext[RunScope],
        agent: str,
        message: str,
        thread: str,
    ) -> str:
        """Ask an agent and wait for its answer, which is returned as this tool's result. This is blocking: your
        reply pauses until the answer arrives (there is no timeout; the agent's run always ends with an answer, a
        failure or a stop). The user can see the thread and reply in it. Files it shares are shown to the user with your reply.
        Ask again in the same thread (same agent and name) to follow up with the agent's context intact, e.g.
        review -> fix -> review again; this also continues a thread opened with `delegate_task`. Several asks in one response run in
        parallel; asks to the same thread share it. If the user writes to you meanwhile, you continue and the
        outcome reaches you later as an event. For long work you want to keep an eye on while doing other things, use
        `delegate_task` with `report_back: true` instead.
        The agent can be yourself (your own name): a fresh conversation of you, with your memory, skills and tools,
        for a self-contained side job (e.g. a code review, or several parts of a task in parallel) whose context
        you do not want in this conversation.

        Args:
            agent: Target agent name from `list_agents`, or your own name.
            message: Self-contained request; the agent sees only this thread's history.
            thread: Short name of the thread, e.g. "code-review". Reuse it with the same agent to continue the
                same conversation (also one opened with `delegate_task`).
        """
        parent_id = ctx.deps.session_id
        target = await agents.find_by_name(agent)
        if not message.strip():
            raise InvalidInput("message must not be empty")
        if ctx.tool_call_id is None:
            raise RuntimeError("ask_agent was called without a tool call id")
        async with db.transaction() as s:
            # 续跑时重新执行这次调用：已经投过就只再挂起（Threads.dispatch 按调用 id 幂等）
            await threads.dispatch(
                s,
                parent_id=parent_id,
                agent=target,
                thread=thread,
                prompt=message,
                call_id=ctx.tool_call_id,
                suspends=True,
                awaited=ctx.deps.user_waits,
            )
        # 发问方的运行停在这次调用上，不占 worker；对方的结局作为这次调用的结果交回（sessions.pending）
        defer_call()

    @ts.tool
    async def stop_thread(ctx: RunContext[RunScope], thread: str, agent: str | None = None) -> str:
        """Stop a thread of this conversation, whether it is running or still queued: its current run stops (what it
        already did is kept) and the work queued for it is dropped. Use it when a check-in or `list_threads` shows
        the work is stuck or no longer needed. If you are waiting for its answer (`ask_agent`, or `report_back`),
        you get its outcome saying it was stopped, with what it said so far.

        Args:
            thread: The thread name used with `delegate_task` or `ask_agent`.
            agent: The thread's agent. Threads are told apart by agent and name: you may leave it out unless you
                opened threads with this name for several agents.
        """
        parent_id = ctx.deps.session_id
        name = validate_thread(thread)
        async with db.transaction() as s:
            named = await threads.named_in(s, parent_id, name)
        if agent is not None:
            target = await agents.find_by_name(agent)
            named = [c for c in named if c.agent_id == target.id]
        if not named:
            whose = "" if agent is None else f" with {agent}"
            raise NotFound(f"this conversation has no thread named {name!r}{whose}; see list_threads")
        if len(named) > 1:
            names = await agents.names()
            raise InvalidInput(
                f"several threads are named {name!r} (agents: {', '.join(names[c.agent_id] for c in named)}); "
                "pass `agent` to say which one"
            )
        [child] = named
        who = f"thread {name!r} ({(await agents.names())[child.agent_id]})"
        if await stop_session(child.id) == Halted.GONE:
            # 查到之后、停止之前被删除了（随所属 agent 删除）
            return f"{who} was deleted meanwhile; nothing to stop"
        return f"stopped {who}"

    @ts.tool
    async def list_threads(ctx: RunContext[RunScope]) -> list[ThreadInfo]:
        """List the threads this conversation has opened (each is one agent + name). Returns a JSON array of \
threads, each with `thread`, `agent`, `state`, `failure` and `answer`. Use it to look at delegated \
work, e.g. after a check-in event, before deciding to keep waiting or `stop_thread`. `state`: running (working \
now), waiting (its run ended but it is waiting for work it started, a wake-up or answers it asked for: not \
finished yet), \
idle (nothing in progress). `answer` is its latest reply; while it is not idle that reply is intermediate, not its \
result. `failure` is set when its latest run failed for good."""
        parent_id = ctx.deps.session_id
        async with db.transaction() as s:
            views = await threads.list_in(s, parent_id)
        # 包括正在删除的 agent（它的 thread 在删除流程里随后删除）
        names = await agents.names()
        return [
            ThreadInfo(
                thread=v.thread,
                agent=names[v.agent_id],
                state=v.progress.state,
                failure=v.progress.failure,
                answer=truncate(v.progress.answer, _ANSWER_PREVIEW) if v.progress.answer else None,
            )
            for v in views
        ]

    return ts
