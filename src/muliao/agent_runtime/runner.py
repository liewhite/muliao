"""执行一次 agent 运行：组装 instructions、toolsets、模型，转发流式事件到总线。"""

import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import Any, Literal
from zoneinfo import ZoneInfo

import anyio
from pydantic_ai import Agent as PaiAgent
from pydantic_ai import (
    AgentRun,
    DeferredToolRequests,
    UsageLimits,
    UserPromptNode,
)
from pydantic_ai.messages import (
    SYNTHESIZED_TOOL_RETURN_METADATA_KEY,
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage
from pydantic_graph import End

from muliao.agent_runtime.attachments import AttachmentsForModel, ToolImagesToFiles
from muliao.agent_runtime.instructions import build_instructions, build_run_context
from muliao.agent_runtime.llm import ModelResolver, check_thinking, model_settings, thinking_text
from muliao.agent_runtime.outcome import Finished, Paused, deferred_tool_results, outcome_of
from muliao.agent_runtime.redaction import tool_input_kept_out_of_failures
from muliao.agent_runtime.tool_errors import DomainErrorsAsRetry
from muliao.agent_runtime.toolbox import RunProfile
from muliao.agent_runtime.transient import transient_as_retryable
from muliao.agents.api import (
    ASSETS_SKILL,
    AgentDir,
    AgentInfo,
    AgentService,
    AssetSource,
    MemoryStore,
    SkillStore,
    Thinking,
)
from muliao.files.api import FileStore
from muliao.platform.db import utcnow
from muliao.platform.tasks import Clock
from muliao.platform.tooling import TOOL_MAX_RETRIES, Audience, RunScope
from muliao.usage.api import RunKind, UsageEntry, UsageLog, UsageTotals

# pydantic-ai 不传 usage_limits 时默认 request_limit=50。运行不设请求数
# 上限：开发任务上百步是常态，运行不设总时长，靠用户停止收束。
_UNLIMITED = UsageLimits(request_limit=None)

log = logging.getLogger(__name__)

TextKind = Literal["assistant", "thinking"]

# 记一笔用量最多等这么久。记账在屏蔽作用域里进行（被取消的运行也花了 token），加时限是为了关停不被它拖住
_BILLING_SECONDS = 10.0


@dataclass
class _Meter:
    """一次模型运行的计量：pydantic-ai 往 usage 里累加每次请求的用量；bill 把上次记上之后新增的部分记一笔（#625：
    逐步运行每次请求后记，会话的累计随步骤更新，服务被硬杀时只丢在途那次请求的）。记不上时 billed 不动，下次连同
    这次的一起记。"""

    bill_delta: Callable[[UsageTotals], Awaitable[bool]]
    usage: RunUsage = field(default_factory=RunUsage)
    billed: UsageTotals = field(default_factory=UsageTotals.zero)

    @property
    def spent(self) -> UsageTotals:
        u = self.usage
        return UsageTotals(
            requests=u.requests,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens,
            cache_write_tokens=u.cache_write_tokens,
        )

    async def bill(self) -> None:
        spent = self.spent
        if await self.bill_delta(spent - self.billed):
            self.billed = spent


@dataclass(frozen=True)
class StepHooks:
    """逐步运行（AgentRuntime.run_steps）时消费者在每一步的两个点上介入（sessions.consumer）。

    运行新增的每条消息恰好交出一次，交出之后归消费者保存（「一次运行新产生了哪些消息」只由 runner 判定，
    docs/architecture.md 2.4 第 1 行）：每次请求之前交出这时新增的（took）、再交出要发出的这次请求（took_request，第一次
    是带着 prompt 的那条）、再回调 before_request，每次回复之后交出这时新增的，驱动以任何方式结束（跑完、停在挂起的
    调用上、失败、被用户停止而取消）时交出其余的（其中可能有 pydantic-ai 补上的工具结果请求，不带 prompt，走 took）。两个
    交出回调都是同步的，与运行记下「交出到哪」之间没有挂起点，被取消时不会有消息两边都不认。交进来的历史（run_steps 的 history）已经
    保存，不再交出；从末尾那条请求续跑时重发的就是它，同样不交出、不回调 before_request。"""

    # 运行开始、第一步之前。notices：工具组连接时发现、要并进这次运行上下文的问题（RunScope.notices，见
    # instructions.with_notices）
    started: Callable[[Sequence[str]], Awaitable[None]]
    # 运行新增、交给消费者保存的消息
    took: Callable[[list[ModelMessage]], None]
    # 要发出的这次请求（交给消费者保存）：只有它带着这次驱动的 prompt（第一次）、上一步的工具结果
    took_request: Callable[[ModelRequest], None]
    # 请求模型之前：request 已经交出、还没发出，可以往里追加内容（新到的信件），并保存已交出的消息
    before_request: Callable[[ModelRequest], Awaitable[None]]
    # 模型回复之后（回复已经交出），工具执行之前：保存已交出的消息
    after_response: Callable[[], Awaitable[None]]

    # 流式展示（会话页）：模型回答与思考的文字增量、工具调用与结果（参数原样，由消费者脱敏，同保存）。都是完整保存之前的预览，保存后
    # 历史里有完整的一份；续跑时交回结果的挂起调用已在历史里，不再报告
    on_text: Callable[[TextKind, str], None]
    on_tool_call: Callable[[ToolCallPart], None]
    on_tool_result: Callable[[ToolReturnPart | RetryPromptPart], None]


class AgentRuntime:
    def __init__(
        self,
        *,
        agents: AgentService,
        assets: AssetSource,
        skills: SkillStore,
        memory: MemoryStore,
        tz: ZoneInfo,
        clock: Clock = utcnow,
        resolve_model: ModelResolver,
        files: FileStore,
        usage: UsageLog,
    ):
        self._agents = agents
        self._assets = assets
        self._skills = skills
        self._memory = memory
        self._tz = tz
        self._clock = clock
        self._resolve_model = resolve_model
        self._usage = usage
        self._pai = PaiAgent(
            deps_type=RunScope,
            output_type=str,
            capabilities=[AttachmentsForModel(files), ToolImagesToFiles(files)],
            # 调用不存在的工具名时 pydantic-ai 用的是 agent 级的工具重试预算（默认 1，第二次就以
            # UnexpectedModelBehavior 终止整轮运行）；这条路径在工具集之外，DomainErrorsAsRetry 管不到，
            # 所以与各工具集取同一个容忍次数（#373）
            retries={"tools": TOOL_MAX_RETRIES},
        )

    def scope(self, agent: AgentInfo, *, session_id: str, audience: Audience) -> RunScope:
        return RunScope(
            agent_id=agent.id,
            workspace=self._agents.workspace(agent.id),
            session_id=session_id,
            audience=audience,
        )

    async def instructions(
        self, agent: AgentDir, scope: RunScope, summary: str | None, origin_note: str
    ) -> str:
        """这次运行的指令。只由会话级、agent 级的稳定内容组成，同一个会话的两次运行得到同一段文字：它是缓存前缀
        （agent_runtime.instructions 的模块说明）。随运行变化的那几行走 run_context。origin_note 是会话来源的说明
        （会话领域给出）。"""
        return build_instructions(
            agent_id=agent.id,
            agent_name=agent.name,
            agent_description=agent.file.description,
            workspace=scope.workspace,
            asset_dir=agent.path,
            origin_note=origin_note,
            memories=await self._memory.for_run(agent.id),
            skills=await self._skills.index(agent.id),
            memory_limit=self._memory.max_chars,
            summary=summary,
        )

    async def config(self, agent_id: str) -> AgentDir:
        """它现在的配置（描述、模型、思考档位）与资源目录，只认 agent.toml：没加载时 NotFound（带上原因），要它的运行当场
        失败，不沿用旧值（模型不能运行的在 _model 失败）。一次会话运行在开头取一份（sessions.consumer），整次运行的各次驱动
        （run_steps）与压缩（summarize）都用它。"""
        return (await self._assets.snapshot()).loaded_agent(agent_id)

    async def _asset_problems(self, agent_id: str) -> list[str]:
        snapshot = await self._assets.snapshot()
        return [
            f"Asset file not loaded: {p.path}: {p.reason}. Fix the file (or tell an admin if you cannot write it); "
            f"formats are in the {ASSETS_SKILL} skill."
            for p in snapshot.problems_for(agent_id)
        ]

    def run_context(self, audience: Audience) -> str:
        """这次运行开头交给模型的运行上下文（当前时间等，见 instructions.build_run_context），由 sessions.steps
        挂到本次新增的第一条请求上。"""
        return build_run_context(now_local=self._clock().astimezone(self._tz), audience=audience)

    async def run_steps(
        self,
        config: AgentDir,
        scope: RunScope,
        prompt: Sequence[UserContent] | None,
        history: Sequence[ModelMessage],
        profile: RunProfile,
        hooks: StepHooks,
        *,
        origin_note: str,
        summary: str | None = None,
        deferred: Mapping[str, object] | None = None,
    ) -> Finished | Paused:
        """会话的一次运行，逐步驱动（pydantic-ai 的 agent.iter）：每次请求模型之前、模型回复之后回调 hooks，消费者
        在那里并入新信件、保存这一步。可以停在挂起的工具调用上（工具调用了 defer_call，如 ask_user，见
        sessions.questions）：结局为 Paused；之后带着 deferred（`{调用 id: 结果}`，如用户的回答）再运行就从那里继续。

        prompt 为 None 时从历史续跑：历史停在一条请求上就重发它（不再交出它，StepHooks），停在带工具调用的回复上就执行这些调用（pydantic-ai
        的续跑语义）。交进来的历史要完整：除了挂起、待重放的，每个调用都已有结果（调用方补齐并存库）。pydantic-ai
        还是动手修补了时当场报错（_unrepaired）：只在内存里修补的历史和库里的对不上，之后的运行会在错位置上再补一次。

        外壳：记账与日志（_billed，每次模型回复后记一笔）、工具失败信息里不带参数（tool_input_kept_out_of_failures）、确认运行从用户 prompt
        节点开始，再由 _step_through 逐步驱动到结束。"""
        # 这次交回结果的挂起调用：调用已在历史里，结果随第一步保存
        resumed = frozenset(deferred or ())
        instructions = await self.instructions(config, scope, summary, origin_note)
        if history and isinstance(last := history[-1], ModelResponse) and last.state == "suspended":
            # 提供方暂停的回复（Anthropic pause_turn 等，只在用内置服务端工具时出现）：pydantic-ai 从它续跑走的是另一条路，
            # 请求不进历史，交出到哪的下标会错位。muliao 不用内置工具，不支持这个起点，当场失败
            raise RuntimeError("cannot resume a run from a provider-suspended response")
        # 这次驱动自己的一份 scope：notices 先放资源目录里与它相关的问题（读坏、名字冲突而没加载的文件），工具组连接时
        # 再往里加；压缩后续跑会重新连接工具组。读过的文件从空开始（压缩后模型看不到之前读到的内容了）
        scope = replace(scope, notices=await self._asset_problems(config.id), reads={})
        ctx = {
            "agent": scope.agent_id,
            "session_id": scope.session_id,
            "model": config.file.model,
            "thinking": config.file.thinking,
            "workspace": str(scope.workspace),
        }
        resolved, settings = self._model(config.file.model, config.file.thinking)
        async with self._billed(
            RunKind.RUN, scope.agent_id, config.file.model, scope.session_id, ctx, len(history)
        ) as meter:
            with tool_input_kept_out_of_failures():
                async with self._pai.iter(
                    None if prompt is None else list(prompt),
                    model=resolved,
                    model_settings=settings,
                    instructions=instructions,
                    message_history=list(history),
                    deps=scope,
                    toolsets=[DomainErrorsAsRetry(t) for t in profile.toolsets],
                    output_type=[str, DeferredToolRequests],
                    deferred_tool_results=None if deferred is None else deferred_tool_results(deferred),
                    usage=meter.usage,
                    usage_limits=_UNLIMITED,
                ) as run:
                    first = run.next_node
                    if not PaiAgent.is_user_prompt_node(first):
                        raise RuntimeError(f"agent run did not start with the user prompt node: {first!r}")
                    await _step_through(run, first, hooks, resumed, scope.notices, history, meter)
                    result = run.result
        if result is None:
            raise RuntimeError("agent run ended without a result")
        return outcome_of(result)

    def _model(self, model: str, thinking: Thinking) -> tuple[Model, ModelSettings | None]:
        """模型与这次运行的 model settings（思考档位、要回思考摘要）。档位会被模型悄悄忽略时报 InvalidInput（llm.check_thinking）：
        agent.toml 里写了不能运行的模型或档位、配置删了 provider、依赖升级后模型 profile 变了，都在这里让运行当场失败
        （读资源树时已记成问题报给它）；每次都查而不分入口，成本只是读 profile。"""
        resolved = self._resolve_model(model)
        check_thinking(model, resolved, thinking)
        return resolved, model_settings(resolved, thinking)

    async def summarize(self, config: AgentDir, session_id: str, instructions: str, prompt: str) -> str:
        """用这份配置的模型做一次无工具的文本生成（压缩 session_id 这个会话的上下文）。不转发事件到前端。"""
        ctx = {
            "agent": config.id,
            "session_id": session_id,
            "model": config.file.model,
            "thinking": config.file.thinking,
        }
        resolved, settings = self._model(config.file.model, config.file.thinking)
        async with self._billed(RunKind.COMPACTION, config.id, config.file.model, session_id, ctx) as meter:
            result = await self._pai.run(
                prompt,
                model=resolved,
                model_settings=settings,
                instructions=instructions,
                usage=meter.usage,
            )
        return result.output

    @asynccontextmanager
    async def _billed(
        self,
        kind: RunKind,
        agent_id: str,
        model: str,
        session_id: str | None,
        ctx: Mapping[str, object],
        history: int | None = None,
    ) -> AsyncIterator[_Meter]:
        """一次模型运行的边界（每个入口都经过它）：记「agent run started」日志（ctx 与带上的历史条数；压缩没有历史这一说，不记），
        交出计量（_Meter：pydantic-ai 往里累加用量，运行中可以先记一笔）；运行无论成功、失败还是被取消，退出时都把还没记上的
        部分记到 session_id 名下，并把整次运行的用量写进「agent run finished」日志。运行里抛出的暂时性失败（模型提供方、
        网络层）在这里换成 ModelUnavailable，交给任务队列按 outage 策略重试（transient）。"""
        log.info("agent run started", extra=dict(ctx) if history is None else {**ctx, "history": history})
        entry = partial(UsageEntry, agent_id=agent_id, session_id=session_id, kind=kind, model=model)
        meter = _Meter(lambda totals: self._bill(entry(totals=totals), ctx))
        try:
            with transient_as_retryable():
                yield meter
        finally:
            await meter.bill()
            log.info("agent run finished", extra={**ctx, **asdict(meter.spent)})

    async def _bill(self, entry: UsageEntry, ctx: Mapping[str, object]) -> bool:
        """记一笔用量；返回这笔是否已入账（没有用量算已入账）。

        用量是账目，不是这次运行的一部分：记不上只记 error 日志，不改变运行的结果或异常——一次记账故障不该让
        已经做完的工作白做，也不该把一个真正的失败换成另一个。没有任何用量（例如还没发出请求就失败了）就跳过。

        放在屏蔽作用域里并加时限：被取消的运行（用户停止、关停）同样花了 token，取消之后也要把它记下来。
        屏蔽挡得住 anyio 的电平取消；用户停止用的原生取消是边沿触发的，对已经被停止的运行来说它在这里之前就
        发出过了，不会再打断收尾那笔写入（sessions.stopping）。运行中那笔等锁时赶上用户停止：原生取消不被
        屏蔽，原样穿出（不是 Exception），_Meter.billed 不动，收尾那笔把它补上。不重复记依赖 UsageLog.record 提交之后
        不再 await。剩下一个窄窗口：收尾那笔正在等数据库锁时用户才按停止，它会随取消丢掉；正常跑完时各次请求已逐笔记上，
        收尾差额通常为 0，不为它加防御。
        """
        if entry.totals == UsageTotals.zero():
            return True
        failed: Exception | None = None
        with anyio.CancelScope(shield=True), anyio.move_on_after(_BILLING_SECONDS) as limited:
            try:
                await self._usage.record(entry)
            except Exception as e:
                failed = e
        if failed is not None:
            log.error("recording model usage failed: %r", failed, extra={**ctx, "kind": entry.kind.value})
            return False
        if limited.cancelled_caught:
            log.error("recording model usage timed out", extra={**ctx, "kind": entry.kind.value})
            return False
        return True


async def _step_through(
    run: AgentRun[RunScope, Any],
    first: UserPromptNode[RunScope, Any],
    hooks: StepHooks,
    resumed: frozenset[str],
    notices: Sequence[str],
    given: Sequence[ModelMessage],
    meter: _Meter,
) -> None:
    """从用户 prompt 节点把运行逐步驱动到结束，在每次请求模型之前、模型回复之后回调 hooks，把运行新增的消息逐步交出
    （StepHooks）。notices 是这次驱动的 scope 里的那份列表：工具组在进入 iter 时已经构建、连接，到这里已经齐了。每次
    回复后先记这次请求的用量再交出、回调 after_response：它保存这一步、通知会话页重新拉取，拉到的累计就含这次请求。

    交出到哪由 handed 记（运行中的历史 ctx.state.message_history 的下标）：pydantic-ai 开始运行时规整过交进来的历史
    （合并相邻请求；重发时取出末尾那条请求），条数可能和交进来的不同，所以基准在规整之后取；之后历史只在末尾追加。
    每次请求在发出时才由 pydantic-ai 追加进历史，它在那之前已经交出（重发的那条已经保存），所以 handed 先越过它，发出之后
    按身份核对历史里那一位就是它（_sent），不符当场报错。下标只在这里用，不交出去。是不是重发只问 pydantic-ai（第一次请求
    节点的 is_resuming_without_prompt）。"""
    node = await run.next(first)
    resend = PaiAgent.is_model_request_node(node) and node.is_resuming_without_prompt
    # 重发时 pydantic-ai 把末尾那条请求从历史里取出、放进第一次请求的节点，一起查
    _unrepaired(given, [*run.ctx.state.message_history, *([node.request] if resend else [])])
    handed = len(run.ctx.state.message_history)

    def take() -> list[ModelMessage]:
        nonlocal handed
        new = run.ctx.state.message_history[handed:]
        handed += len(new)
        if new:
            hooks.took(new)
        return new

    await hooks.started(notices)
    try:
        while not isinstance(node, End):
            if PaiAgent.is_model_request_node(node):
                if take() and resend:
                    raise RuntimeError("messages appeared before resending the last request")
                request = node.request
                # 这条请求在发出时才由 pydantic-ai 追加进历史：重发的那条已经保存，其余的现在交出（与 handed 越过它之间
                # 没有挂起点），由 before_request 并进内容、保存
                handed += 1
                if resend:
                    resend = False
                else:
                    hooks.took_request(request)
                    await hooks.before_request(request)
                async with node.stream(run.ctx) as stream:
                    await _forward_events(stream, hooks, resumed)
                _sent(run.ctx.state.message_history, handed - 1, request)
                node = await run.next(node)
                await meter.bill()
                new = take()
                if not new or not isinstance(new[-1], ModelResponse):
                    raise RuntimeError(f"model request ended without a response, it added {new!r}")
                await hooks.after_response()
            elif PaiAgent.is_call_tools_node(node):
                async with node.stream(run.ctx) as stream:
                    await _forward_events(stream, hooks, resumed)
                node = await run.next(node)
            else:
                node = await run.next(node)
    finally:
        take()


def _sent(history: Sequence[ModelMessage], at: int, request: ModelRequest) -> None:
    """发出之后运行中的历史第 at 位就是这条请求（同一个对象）：pydantic-ai 每次请求恰好在末尾追加它一条，交出到哪
    的下标靠这条成立，不符就当场失败。"""
    if at >= len(history) or history[at] is not request:
        raise RuntimeError(
            f"pydantic-ai did not append the sent request at {at} (history has {len(history)} messages)"
        )


def _unrepaired(given: Sequence[ModelMessage], messages: Sequence[ModelMessage]) -> None:
    """pydantic-ai 开始运行时规整历史（given → messages，含取出待重发的请求）没有动工具结果：没给悬空的调用合成结果，
    也没丢掉找不到调用的结果。它静默地做、只在内存里：做了就是交进来的历史不合规则、和库里的对不上，当场失败。"""
    if made_up := [
        p.tool_call_id
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart) and (p.metadata or {}).get(SYNTHESIZED_TOOL_RETURN_METADATA_KEY)
    ]:
        raise RuntimeError(
            f"pydantic-ai synthesized results for dangling tool calls in the history: {made_up}"
        )
    if dropped := _result_ids(given) - _result_ids(messages):
        raise RuntimeError(f"pydantic-ai dropped tool results from the history: {sorted(dropped)}")


def _result_ids(messages: Sequence[ModelMessage]) -> set[str]:
    """工具调用的结果 id（不带工具名的 RetryPromptPart 是对整条回复的重试提示，不针对调用）。"""
    return {
        p.tool_call_id
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart) or (isinstance(p, RetryPromptPart) and p.tool_name is not None)
    }


def _grown(before: str, after: str) -> str:
    """一个思考块给人看的文字从 before 变成 after 时要追加展示的部分：通常是末尾新长出的一段。先回了原始推理、后来
    才有摘要正文的思考块，给人看的文字从原始推理换成了摘要（thinking_text 正文优先），不是在原来的后面接着长：
    把换成的文字整段追加，实时展示里先前的原始推理留着，保存后的历史里只有摘要。"""
    return after[len(before) :] if after.startswith(before) else after


async def _forward_events(
    stream: AsyncIterable[AgentStreamEvent], hooks: StepHooks, resumed: frozenset[str]
) -> None:
    """把一步里的流式事件报告给 hooks（会话页的实时展示）。续跑时 pydantic-ai 会把交回结果的挂起调用（resumed）再发
    一遍调用与结果事件：它们已经在历史里，不报告。

    思考报告的是给人看的文字（llm.thinking_text）新长出来的那一段，和保存后历史里展示的同一个依据：它不一定是正文的
    增量（原始推理文字的增量是对 provider_details 的一次更新，事件里没有那段文字），所以把这一步的思考块按序号跟着
    累计，比较前后。"""
    thoughts: dict[int, ThinkingPart] = {}
    async for event in stream:
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart) and event.part.content:
            hooks.on_text("assistant", event.part.content)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            hooks.on_text("assistant", event.delta.content_delta)
        elif isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
            thoughts[event.index] = event.part
            if shown := thinking_text(event.part):
                hooks.on_text("thinking", shown)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
            before = thoughts[event.index]
            after = thoughts[event.index] = event.delta.apply(before)
            # 只更新签名的增量没有新文字，不报告
            if grown := _grown(thinking_text(before), thinking_text(after)):
                hooks.on_text("thinking", grown)
        elif isinstance(event, FunctionToolCallEvent | FunctionToolResultEvent) and (
            event.part.tool_call_id in resumed
        ):
            continue
        elif isinstance(event, FunctionToolCallEvent):
            hooks.on_tool_call(event.part)
        elif isinstance(event, FunctionToolResultEvent):
            hooks.on_tool_result(event.part)
