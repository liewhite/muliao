"""执行一次 agent 运行：组装 instructions、toolsets、模型，转发流式事件到总线。"""

import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import anyio
from pydantic_ai import Agent as PaiAgent
from pydantic_ai import (
    AgentRun,
    DeferredToolRequests,
    DeferredToolResults,
    FunctionToolset,
    UsageLimits,
    UserPromptNode,
)
from pydantic_ai.messages import (
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
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage
from pydantic_graph import End

from muliao.agent_runtime.attachments import AttachmentsForModel, ToolImagesToFiles
from muliao.agent_runtime.instructions import build_instructions, build_run_context
from muliao.agent_runtime.llm import ModelResolver, check_thinking, model_settings
from muliao.agent_runtime.outcome import Finished, Paused, outcome_of
from muliao.agent_runtime.redaction import redact_call, redact_retry, tool_input_kept_out_of_failures
from muliao.agent_runtime.tool_errors import DomainErrorsAsRetry
from muliao.agent_runtime.transient import transient_as_retryable
from muliao.agents.api import AgentInfo, AgentService, MemoryStore, Rules, SkillStore, Thinking
from muliao.files.api import FileStore
from muliao.platform.db import utcnow
from muliao.platform.tasks import Clock
from muliao.platform.tooling import Audience, RunScope
from muliao.usage.api import RunKind, UsageEntry, UsageLog, UsageTotals

# pydantic-ai 不传 usage_limits 时默认 request_limit=50。运行不设请求数
# 上限：开发任务上百步是常态，运行不设总时长，靠用户停止收束。
_UNLIMITED = UsageLimits(request_limit=None)

log = logging.getLogger(__name__)

TextKind = Literal["assistant", "thinking"]

# 运行结束后记用量最多等这么久。记账在屏蔽作用域里进行（被取消的运行也花了 token），加时限是为了关停不被它拖住
_BILLING_SECONDS = 10.0


@dataclass(frozen=True)
class StepHooks:
    """逐步运行（AgentRuntime.run_steps）时消费者在每一步的两个点上介入（sessions.consumer）。

    messages 是运行中的历史本身（pydantic-ai 的 ctx.state.message_history，每次回调都取当前的那个列表）：被取消
    （用户停止）之后，消费者从它取回还没保存的部分。"""

    # 运行开始、第一步之前：pydantic-ai 已经规整过传入的历史（合并相邻请求、补齐悬空调用的结果；从请求续跑时取出
    # 末尾那条请求准备重发），条数可能和传入的不同。之后历史只在末尾追加，这里的条数就是「已经保存」的基准。
    # notices：工具组连接时发现、要并进这次运行上下文的问题（RunScope.notices，见 instructions.with_notices）
    started: Callable[[list[ModelMessage], Sequence[str]], Awaitable[None]]

    # 请求模型之前：request 还没加进 history，可以往里追加内容（新到的信件），并保存它
    before_request: Callable[[ModelRequest, list[ModelMessage]], Awaitable[None]]
    # 模型回复之后（回复已在 history 末尾），工具执行之前：保存它
    after_response: Callable[[ModelResponse, list[ModelMessage]], Awaitable[None]]

    # 流式展示（会话页）：模型回答与思考的文字增量、工具调用（参数已脱敏）与结果。都是完整保存之前的预览，保存后
    # 历史里有完整的一份；续跑时交回结果的挂起调用已在历史里，不再报告
    on_text: Callable[[TextKind, str], None]
    on_tool_call: Callable[[ToolCallPart], None]
    on_tool_result: Callable[[ToolReturnPart | RetryPromptPart], None]


@dataclass(frozen=True)
class RunProfile:
    """一次运行开放的工具：从 Toolbox 里按 agent 的权限规则取。"""

    toolsets: Sequence[AbstractToolset[RunScope]]


@dataclass(frozen=True)
class Toolbox:
    """运行可用的全部工具：静态的工具集（每个工具的权限见 agents.permissions.TOOL_PERMISSIONS）与按运行构建的
    MCP。一次运行开放哪些在运行开始时算定（profile），调用时不再判断：静态工具按 agent 的权限规则，MCP 不归权限管，
    是 agent 的私有 server 与它勾选的公共 server（AgentInfo.mcp_servers）。"""

    toolsets: Sequence[FunctionToolset[RunScope]]
    # MCP 工具：按勾选的公共 server 名构建，每次运行按 agent 可用的 server 连接
    mcp: Callable[[Sequence[str]], AbstractToolset[RunScope]]
    # agent 现在的权限规则（AgentService.rules：角色按现在的定义展开）
    rules_of: Callable[[AgentInfo], Awaitable[Rules]]

    def tool_names(self) -> list[str]:
        return [name for ts in self.toolsets for name in ts.tools]

    async def profile(self, agent: AgentInfo) -> RunProfile:
        allowed = set((await self.rules_of(agent)).tools(self.tool_names()))
        static: list[AbstractToolset[RunScope]] = [
            ts.filtered(lambda _ctx, td: td.name in allowed)
            for ts in self.toolsets
            if allowed & set(ts.tools)
        ]
        return RunProfile(toolsets=[*static, self.mcp(agent.mcp_servers)])


class AgentRuntime:
    def __init__(
        self,
        *,
        agents: AgentService,
        skills: SkillStore,
        memory: MemoryStore,
        tz: ZoneInfo,
        clock: Clock = utcnow,
        resolve_model: ModelResolver,
        files: FileStore,
        usage: UsageLog,
    ):
        self._agents = agents
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
        )

    def workspace(self, agent_id: str) -> Path:
        return self._agents.workspace(agent_id)

    def scope(self, agent: AgentInfo, *, session_id: str, audience: Audience) -> RunScope:
        return RunScope(
            agent_id=agent.id, workspace=self.workspace(agent.id), session_id=session_id, audience=audience
        )

    async def instructions(
        self, agent: AgentInfo, scope: RunScope, summary: str | None, origin_note: str
    ) -> str:
        """这次运行的指令。只由会话级、agent 级的稳定内容组成，同一个会话的两次运行得到同一段文字：它是缓存前缀
        （agent_runtime.instructions 的模块说明）。随运行变化的那几行走 run_context。origin_note 是会话来源的说明
        （会话领域给出）。"""
        skills, corrupt_skills = self._skills.index(agent.id)
        return build_instructions(
            agent_name=agent.name,
            agent_description=agent.description,
            global_memory=self._memory.read(None),
            workspace=scope.workspace,
            origin_note=origin_note,
            memory=self._memory.read(agent.id),
            skills=skills,
            corrupt_skills=corrupt_skills,
            memory_limit=self._memory.max_chars,
            summary=summary,
        )

    def run_context(self, audience: Audience) -> str:
        """这次运行开头交给模型的运行上下文（当前时间等，见 instructions.build_run_context），由 sessions.steps
        挂到本次新增的第一条请求上。"""
        return build_run_context(now_local=self._clock().astimezone(self._tz), audience=audience)

    async def run_steps(
        self,
        agent: AgentInfo,
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

        prompt 为 None 时从历史续跑：历史停在一条请求上就重发它，停在带工具调用的回复上就执行这些调用（pydantic-ai
        的续跑语义）。

        外壳：记账与日志（_billed）、工具失败信息里不带参数（tool_input_kept_out_of_failures）、确认运行从用户 prompt
        节点开始，再由 _step_through 逐步驱动到结束。"""
        # 这次交回结果的挂起调用：调用已在历史里，结果随第一步保存
        resumed = frozenset(deferred or ())
        instructions = await self.instructions(agent, scope, summary, origin_note)
        # 这次驱动自己的一份 scope：notices 为空，工具组连接时往里加；压缩后续跑会重新连接工具组
        scope = replace(scope, notices=[])
        ctx = {
            "agent": scope.agent_id,
            "session_id": scope.session_id,
            "model": agent.model,
            "thinking": agent.thinking,
            "workspace": str(scope.workspace),
        }
        resolved, settings = self._model(agent.model, agent.thinking)
        async with self._billed(
            RunKind.RUN, scope.agent_id, agent.model, scope.session_id, ctx, len(history)
        ) as usage:
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
                    deferred_tool_results=None
                    if deferred is None
                    else DeferredToolResults(calls=dict(deferred)),
                    usage=usage,
                    usage_limits=_UNLIMITED,
                ) as run:
                    first = run.next_node
                    if not PaiAgent.is_user_prompt_node(first):
                        raise RuntimeError(f"agent run did not start with the user prompt node: {first!r}")
                    await _step_through(run, first, hooks, resumed, scope.notices)
                    result = run.result
        if result is None:
            raise RuntimeError("agent run ended without a result")
        return outcome_of(result)

    def _model(self, model: str, thinking: Thinking) -> tuple[Model, ModelSettings | None]:
        """模型与这次运行的 model settings（思考档位）。档位会被模型悄悄忽略时报 InvalidInput（llm.check_thinking）：
        agent 存下的档位在写入与启动时已查过，这里拦住的是依赖升级后模型 profile 变了的情况；每次都查而不分入口，
        成本只是读 profile。"""
        resolved = self._resolve_model(model)
        check_thinking(model, resolved, thinking)
        return resolved, model_settings(thinking)

    async def summarize(self, agent: AgentInfo, session_id: str, instructions: str, prompt: str) -> str:
        """用该 agent 的模型做一次无工具的文本生成（压缩 session_id 这个会话的上下文）。不转发事件到前端。"""
        ctx = {"agent": agent.id, "session_id": session_id, "model": agent.model, "thinking": agent.thinking}
        resolved, settings = self._model(agent.model, agent.thinking)
        async with self._billed(RunKind.COMPACTION, agent.id, agent.model, session_id, ctx) as usage:
            result = await self._pai.run(
                prompt,
                model=resolved,
                model_settings=settings,
                instructions=instructions,
                usage=usage,
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
    ) -> AsyncIterator[RunUsage]:
        """一次模型运行的边界（每个入口都经过它）：记「agent run started」日志（ctx 与带上的历史条数；压缩没有历史这一说，不记），
        交出给 pydantic-ai 累加用量的 RunUsage；运行无论成功、失败还是被取消，退出时都把已花掉的部分记到 session_id 名下
        （_bill）。运行里抛出的暂时性失败（模型提供方、网络层）在这里换成 ModelUnavailable，交给任务队列按 outage 策略重试（transient）。"""
        log.info("agent run started", extra=dict(ctx) if history is None else {**ctx, "history": history})
        usage = RunUsage()
        try:
            with transient_as_retryable():
                yield usage
        finally:
            await self._bill(kind, agent_id, session_id, model, usage, ctx)

    async def _bill(
        self,
        kind: RunKind,
        agent_id: str,
        session_id: str | None,
        model: str,
        usage: RunUsage,
        ctx: Mapping[str, object],
    ) -> None:
        """记下这次模型运行花掉的 token，并把结果写进「agent run finished」日志。

        用量是账目，不是这次运行的一部分：记不上只记 error 日志，不改变运行的结果或异常——一次记账故障不该让
        已经做完的工作白做，也不该把一个真正的失败换成另一个。没有任何用量（例如还没发出请求就失败了）就跳过。

        放在屏蔽作用域里并加时限：被取消的运行（用户停止、关停）同样花了 token，取消之后也要把它记下来。
        屏蔽挡得住 anyio 的电平取消；用户停止用的原生取消是边沿触发的，对已经被停止的运行来说它在这里之前就
        发出过了，不会再打断这次写入（sessions.stopping）。剩下一个窄窗口：运行正常跑完、正在这里等数据库锁时
        用户才按停止，这一条用量会随取消一起丢掉。不为它加防御——结局和「停止早到一瞬」一样，消息都已逐步保存，
        代价只是一条账目。
        """
        totals = UsageTotals(
            requests=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
        log.info("agent run finished", extra={**ctx, **asdict(totals)})
        if totals == UsageTotals.zero():
            return
        entry = UsageEntry(agent_id=agent_id, session_id=session_id, kind=kind, model=model, totals=totals)
        failed: Exception | None = None
        with anyio.CancelScope(shield=True), anyio.move_on_after(_BILLING_SECONDS) as limited:
            try:
                await self._usage.record(entry)
            except Exception as e:
                failed = e
        if failed is not None:
            log.error("recording model usage failed: %r", failed, extra={**ctx, "kind": kind.value})
        elif limited.cancelled_caught:
            log.error("recording model usage timed out", extra={**ctx, "kind": kind.value})


async def _step_through(
    run: AgentRun[RunScope, Any],
    first: UserPromptNode[RunScope, Any],
    hooks: StepHooks,
    resumed: frozenset[str],
    notices: Sequence[str],
) -> None:
    """从用户 prompt 节点把运行逐步驱动到结束，在每次请求模型之前、模型回复之后回调 hooks。notices 是这次驱动的
    scope 里的那份列表：工具组在进入 iter 时已经构建、连接，到这里已经齐了。"""
    node = await run.next(first)
    await hooks.started(run.ctx.state.message_history, notices)
    while not isinstance(node, End):
        if PaiAgent.is_model_request_node(node):
            await hooks.before_request(node.request, run.ctx.state.message_history)
            async with node.stream(run.ctx) as stream:
                await _forward_events(stream, hooks, resumed)
            node = await run.next(node)
            messages = run.ctx.state.message_history
            response = messages[-1]
            if not isinstance(response, ModelResponse):
                raise RuntimeError(f"model request ended without a response, history ends with {response!r}")
            await hooks.after_response(response, messages)
        elif PaiAgent.is_call_tools_node(node):
            async with node.stream(run.ctx) as stream:
                await _forward_events(stream, hooks, resumed)
            node = await run.next(node)
        else:
            node = await run.next(node)


async def _forward_events(
    stream: AsyncIterable[AgentStreamEvent], hooks: StepHooks, resumed: frozenset[str]
) -> None:
    """把一步里的流式事件报告给 hooks（会话页的实时展示）。续跑时 pydantic-ai 会把交回结果的挂起调用（resumed）再发
    一遍调用与结果事件：它们已经在历史里，不报告。"""
    async for event in stream:
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart) and event.part.content:
            hooks.on_text("assistant", event.part.content)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            hooks.on_text("assistant", event.delta.content_delta)
        elif (
            isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart) and event.part.content
        ):
            hooks.on_text("thinking", event.part.content)
        # 只更新签名、没有新内容的增量（content_delta 为 None）不用展示
        elif (
            isinstance(event, PartDeltaEvent)
            and isinstance(event.delta, ThinkingPartDelta)
            and event.delta.content_delta
        ):
            hooks.on_text("thinking", event.delta.content_delta)
        elif isinstance(event, FunctionToolCallEvent | FunctionToolResultEvent) and (
            event.part.tool_call_id in resumed
        ):
            continue
        elif isinstance(event, FunctionToolCallEvent):
            hooks.on_tool_call(redact_call(event.part))
        elif isinstance(event, FunctionToolResultEvent):
            part = event.part
            hooks.on_tool_result(redact_retry(part) if isinstance(part, RetryPromptPart) else part)
