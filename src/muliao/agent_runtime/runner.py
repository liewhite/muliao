"""执行一次 agent 运行：组装 instructions、toolsets、模型，转发流式事件到总线。"""

import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import anyio
from pydantic_ai import Agent as PaiAgent
from pydantic_ai import AgentRunResult, DeferredToolRequests, DeferredToolResults, RunContext, UsageLimits
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
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage
from pydantic_graph import End

from muliao.agent_runtime.answers import Answer
from muliao.agent_runtime.attachments import AttachmentsForModel
from muliao.agent_runtime.instructions import (
    build_instructions,
    build_run_context,
    build_temp_instructions,
    with_notices,
)
from muliao.agent_runtime.llm import ModelResolver
from muliao.agent_runtime.redaction import redact_call, redact_retry, tool_input_kept_out_of_failures
from muliao.agents.api import AgentInfo, AgentService, MemoryStore, PromptStore, SkillStore
from muliao.files.api import FileStore
from muliao.platform.db import utcnow
from muliao.platform.tasks import Clock
from muliao.platform.tooling import Audience, DomainErrorsAsRetry, Origin, RunScope
from muliao.usage.api import RunKind, UsageEntry, UsageLog

# pydantic-ai 不传 usage_limits 时默认 request_limit=50。任何运行（长期 agent 的轮次与反思、临时 agent）都不设请求数
# 上限：开发任务上百步是常态，运行不设总时长，靠用户停止收束（反思见 architecture.md「TaskHandler 接口」）。
_UNLIMITED = UsageLimits(request_limit=None)

log = logging.getLogger(__name__)

TextKind = Literal["assistant", "thinking"]

# 运行结束后记用量最多等这么久。记账在屏蔽作用域里进行（被取消的运行也花了 token），加时限是为了关停不被它拖住
_BILLING_SECONDS = 10.0

EventHandler = Callable[[RunContext[RunScope], AsyncIterable[AgentStreamEvent]], Awaitable[None]]


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
    """一次运行开放的工具：对话按 agent 选用的工具组组合（ToolGroups），反思只开放自我改进（加上 agent 已有的写公共
    skill），临时 agent 只开放文件与 shell（加调用方可见的 MCP）。"""

    toolsets: Sequence[AbstractToolset[RunScope]]


@dataclass(frozen=True)
class ToolGroups:
    """agent 按名称选用的工具组（组名见 tool_catalog），每组是若干工具集。always 里的（自我改进：私有 skill 与
    memory、提问）对每个 agent 都开放。

    用途是限制处理不可信输入的 agent 能做什么（例如处理外部事件的 agent 只开放通知）。
    """

    groups: Mapping[str, Sequence[AbstractToolset[RunScope]]]
    always: Sequence[AbstractToolset[RunScope]]

    def profile(
        self, agent_id: str, names: Sequence[str], *, within: Collection[str] | None = None
    ) -> RunProfile:
        """按 agent 选用的组组合工具；within 给出时只取其中这些组（反思只可能用到写公共 skill）。"""
        if within is not None and not set(within) <= set(self.groups):
            raise RuntimeError(
                f"restriction names unknown tool groups {sorted(set(within) - set(self.groups))}"
            )
        unknown = [n for n in names if n not in self.groups]
        if unknown:
            # 写入时已校验（AgentService）；读到未知组说明数据与代码不一致
            raise RuntimeError(f"agent {agent_id} references unknown tool groups {unknown}")
        chosen = [self.groups[n] for n in names if within is None or n in within]
        return RunProfile(toolsets=[*self.always, *(t for g in chosen for t in g)])


class AgentRuntime:
    def __init__(
        self,
        *,
        agents: AgentService,
        skills: SkillStore,
        memory: MemoryStore,
        prompts: PromptStore,
        tz: ZoneInfo,
        clock: Clock = utcnow,
        resolve_model: ModelResolver,
        files: FileStore,
        usage: UsageLog,
    ):
        self._agents = agents
        self._skills = skills
        self._memory = memory
        self._prompts = prompts
        self._tz = tz
        self._clock = clock
        self._resolve_model = resolve_model
        self._usage = usage
        self._pai = PaiAgent(deps_type=RunScope, output_type=str, capabilities=[AttachmentsForModel(files)])

    def workspace(self, agent_id: str) -> Path:
        return self._agents.workspace(agent_id)

    def scope(
        self, agent: AgentInfo, *, session_id: str | None, origin: Origin | None, audience: Audience | None
    ) -> RunScope:
        return RunScope(
            agent_id=agent.id,
            workspace=self.workspace(agent.id),
            session_id=session_id,
            origin=origin,
            audience=audience,
        )

    async def instructions(self, agent: AgentInfo, scope: RunScope, summary: str | None) -> str:
        """这次运行的指令。只由会话级、agent 级的稳定内容组成，同一个会话的两次运行得到同一段文字：它是缓存前缀
        （agent_runtime.instructions 的模块说明）。随运行变化的那几行走 run_context。"""
        return build_instructions(
            agent_name=agent.name,
            agent_description=agent.description,
            public_instructions=self._prompts.current(None),
            agent_instructions=self._prompts.current(agent.id),
            workspace=scope.workspace,
            origin=scope.origin,
            memory=self._memory.read(agent.id),
            skills=self._skills.visible_to(agent.id),
            corrupt_skills=self._skills.corrupt(agent.id),
            memory_limit=self._memory.max_chars,
            summary=summary,
        )

    def run_context(self, audience: Audience | None) -> str:
        """这次运行开头交给模型的运行上下文（当前时间等，见 instructions.build_run_context）。会话的运行由
        sessions.steps 挂到本次新增的第一条请求上；没有历史的运行（反思、临时 agent）直接放在 prompt 前面。"""
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
        summary: str | None = None,
        deferred: DeferredToolResults | None = None,
    ) -> AgentRunResult[str | DeferredToolRequests]:
        """会话的一次运行，逐步驱动（pydantic-ai 的 agent.iter）：每次请求模型之前、模型回复之后回调 hooks，消费者
        在那里并入新信件、保存这一步。可以停在挂起的工具调用上（ask_user，见 sessions.questions）：输出为
        DeferredToolRequests；之后带着 deferred（用户的回答）再运行就从那里继续。

        prompt 为 None 时从历史续跑：历史停在一条请求上就重发它，停在带工具调用的回复上就执行这些调用（pydantic-ai
        的续跑语义）。"""
        model = agent.model
        session_id = scope.session_id
        if session_id is None:
            raise RuntimeError(f"a stepwise run needs a session, agent {scope.agent_id} has none")
        ctx = {
            "agent": scope.agent_id,
            "session_id": session_id,
            "model": model,
            "workspace": str(scope.workspace),
        }
        # 每次驱动一份 notices：压缩后续跑会重新连接工具组
        scope = replace(scope, notices=[])
        # 这次交回结果的挂起调用：调用已在历史里，结果随第一步保存
        resumed = frozenset(deferred.calls) if deferred is not None else frozenset()
        async with self._billed(RunKind.RUN, scope.agent_id, model, session_id, ctx, len(history)) as usage:
            with tool_input_kept_out_of_failures():
                async with self._pai.iter(
                    None if prompt is None else list(prompt),
                    model=self._resolve_model(model),
                    instructions=await self.instructions(agent, scope, summary),
                    message_history=list(history),
                    deps=scope,
                    toolsets=self._toolsets(profile),
                    output_type=[str, DeferredToolRequests],
                    deferred_tool_results=deferred,
                    usage=usage,
                    usage_limits=_UNLIMITED,
                ) as run:
                    node = run.next_node
                    if not PaiAgent.is_user_prompt_node(node):
                        raise RuntimeError(f"agent run did not start with the user prompt node: {node!r}")
                    node = await run.next(node)
                    # 工具组在进入 iter 时已经构建、连接（notices 已经齐了）
                    await hooks.started(run.ctx.state.message_history, scope.notices)
                    while not isinstance(node, End):
                        if PaiAgent.is_model_request_node(node):
                            await hooks.before_request(node.request, run.ctx.state.message_history)
                            async with node.stream(run.ctx) as stream:
                                await _forward_events(stream, hooks, resumed)
                            node = await run.next(node)
                            messages = run.ctx.state.message_history
                            response = messages[-1]
                            if not isinstance(response, ModelResponse):
                                raise RuntimeError(
                                    f"model request ended without a response, history ends with {response!r}"
                                )
                            await hooks.after_response(response, messages)
                        elif PaiAgent.is_call_tools_node(node):
                            async with node.stream(run.ctx) as stream:
                                await _forward_events(stream, hooks, resumed)
                            node = await run.next(node)
                        else:
                            node = await run.next(node)
                    result = run.result
        if result is None:
            raise RuntimeError("agent run ended without a result")
        return result

    async def run_once(
        self, agent: AgentInfo, scope: RunScope, prompt: str, profile: RunProfile
    ) -> AgentRunResult[str]:
        """agent 一次不属于会话的运行（反思）：带它完整的指令，没有历史，不逐步保存。"""
        return await self._execute(
            model=agent.model,
            instructions=await self.instructions(agent, scope, None),
            toolsets=self._toolsets(profile),
            scope=scope,
            context=self.run_context(scope.audience),
            prompt=prompt,
            history=(),
            kind=RunKind.REFLECTION,
            billed_to=scope.session_id,
        )

    async def run_temp(
        self,
        parent: AgentInfo,
        *,
        task: str,
        instructions: str,
        model: str,
        profile: RunProfile,
        workspace: Path,
        session_id: str | None,
    ) -> Answer:
        """一次性的临时 agent：替 parent 做一件事，返回最终回答，以及它用工具挂给用户的文件（如 share_file）。

        没有记忆、skill、历史与反思；工具来自 profile（不含委派与再开临时 agent），MCP 与 secret 按 parent 的视角。
        也不带公共 prompt：公共 prompt 约定的是长期 agent 之间的共同规则，临时 agent 只执行调用方交给它的任务。
        不属于任何会话，运行事件不推到会话页。和长期 agent 一样不设请求数上限。

        session_id 是发起它的那个会话：临时 agent 自己不属于任何会话（RunScope 里仍是 None），但它花的 token
        是那次运行的开销，记到那个会话名下。
        """
        scope = RunScope(agent_id=parent.id, workspace=workspace, session_id=None, origin=None, audience=None)
        result = await self._execute(
            model=model,
            instructions=build_temp_instructions(
                parent_name=parent.name, workspace=workspace, extra=instructions
            ),
            toolsets=self._toolsets(profile),
            # 临时 agent 没有听众（回答交回调用方那次工具调用），运行上下文只有当前时间
            context=self.run_context(None),
            prompt=task,
            scope=scope,
            history=(),
            kind=RunKind.TEMP_AGENT,
            billed_to=session_id,
        )
        return Answer.of_run(result.output, result.new_messages())

    @staticmethod
    def _toolsets(profile: RunProfile) -> list[AbstractToolset[RunScope]]:
        return [DomainErrorsAsRetry(t) for t in profile.toolsets]

    async def _execute(
        self,
        *,
        model: str,
        instructions: str,
        toolsets: list[AbstractToolset[RunScope]],
        scope: RunScope,
        context: str,
        prompt: str,
        history: Sequence[ModelMessage],
        kind: RunKind,
        billed_to: str | None,
    ) -> AgentRunResult[str]:
        """没有历史逐步保存的一次运行：运行上下文（context）放在 prompt 前面。"""
        scope = replace(scope, notices=[])
        ctx = {
            "agent": scope.agent_id,
            "session_id": scope.session_id,
            "model": model,
            "workspace": str(scope.workspace),
        }
        async with self._billed(kind, scope.agent_id, model, billed_to, ctx, len(history)) as usage:
            with tool_input_kept_out_of_failures():
                async with self._pai.iter(
                    f"{context}\n\n{prompt}",
                    model=self._resolve_model(model),
                    instructions=instructions,
                    message_history=list(history),
                    deps=scope,
                    toolsets=toolsets,
                    usage_limits=_UNLIMITED,
                    usage=usage,
                ) as run:
                    node = run.next_node
                    if not PaiAgent.is_user_prompt_node(node):
                        raise RuntimeError(f"agent run did not start with the user prompt node: {node!r}")
                    if scope.notices:
                        # 工具组在进入 iter 时已经连接，notices 这时才齐；prompt 还没交给模型
                        node.user_prompt = f"{with_notices(context, scope.notices)}\n\n{prompt}"
                    async for _ in run:
                        pass
                    result = run.result
        if result is None:
            raise RuntimeError("agent run ended without a result")
        return result

    async def summarize(self, agent: AgentInfo, session_id: str, instructions: str, prompt: str) -> str:
        """用该 agent 的模型做一次无工具的文本生成（压缩 session_id 这个会话的上下文）。不转发事件到前端。"""
        ctx = {"agent": agent.id, "session_id": session_id, "model": agent.model}
        async with self._billed(RunKind.COMPACTION, agent.id, agent.model, session_id, ctx) as usage:
            result = await self._pai.run(
                prompt, model=self._resolve_model(agent.model), instructions=instructions, usage=usage
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
        """一次模型运行的记账：记「agent run started」日志（ctx 与带上的历史条数；压缩没有历史这一说，不记），交出给 pydantic-ai 累加用量的
        RunUsage；运行无论成功、失败还是被取消，退出时都把已花掉的部分记到 session_id 名下（_bill）。"""
        log.info("agent run started", extra=dict(ctx) if history is None else {**ctx, "history": history})
        usage = RunUsage()
        try:
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
        log.info(
            "agent run finished",
            extra={
                **ctx,
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
            },
        )
        if not (usage.requests or usage.input_tokens or usage.output_tokens):
            return
        entry = UsageEntry(
            agent_id=agent_id,
            session_id=session_id,
            kind=kind,
            model=model,
            requests=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
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
