"""执行一次 agent 运行：组装 instructions、toolsets、模型，转发流式事件到总线。"""

import logging
from collections.abc import AsyncIterable, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

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
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolReturnPart,
    UserContent,
)
from pydantic_ai.toolsets import AbstractToolset
from pydantic_graph import End

from muliao.agents import AgentService
from muliao.bus import LiveRuns
from muliao.conventions import tool_attachments, tool_threads
from muliao.db import utcnow
from muliao.evolution.memory import MemoryStore
from muliao.evolution.prompts import PromptStore
from muliao.evolution.skills import SkillStore
from muliao.files import FileStore
from muliao.llm import ModelResolver
from muliao.models import Agent, Origin, ReplyTo
from muliao.runtime.answers import Answer
from muliao.runtime.attachments import AttachmentsForModel
from muliao.runtime.instructions import build_instructions, build_temp_instructions
from muliao.runtime.mcp import McpRegistry
from muliao.runtime.redaction import redact_call
from muliao.runtime.scope import RunScope
from muliao.runtime.tasks import Clock
from muliao.runtime.tools.errors import DomainErrorsAsRetry
from muliao.transcript import truncate

log = logging.getLogger(__name__)

_EVENT_TEXT_LIMIT = 2000


EventHandler = Callable[[RunContext[RunScope], AsyncIterable[AgentStreamEvent]], Awaitable[None]]


@dataclass(frozen=True)
class StepHooks:
    """逐步运行（AgentRuntime.run_steps）时消费者在每一步的两个点上介入（runtime.consumer）。

    messages 是运行中的历史本身（pydantic-ai 的 ctx.state.message_history，每次回调都取当前的那个列表）：被取消
    （用户停止）之后，消费者从它取回还没保存的部分。"""

    # 运行开始、第一步之前：pydantic-ai 已经规整过传入的历史（合并相邻请求、补齐悬空调用的结果；从请求续跑时取出
    # 末尾那条请求准备重发），条数可能和传入的不同。之后历史只在末尾追加，这里的条数就是「已经保存」的基准
    started: Callable[[list[ModelMessage]], Awaitable[None]]

    # 请求模型之前：request 还没加进 history，可以往里追加内容（新到的信件），并保存它
    before_request: Callable[[ModelRequest, list[ModelMessage]], Awaitable[None]]
    # 模型回复之后（回复已在 history 末尾），工具执行之前：保存它
    after_response: Callable[[ModelResponse, list[ModelMessage]], Awaitable[None]]


@dataclass(frozen=True)
class RunProfile:
    """一次运行开放的能力：对话按 agent 选用的工具组组合（ToolGroups），反思只开放自我改进（加上 agent 已有的写公共 skill），
    临时 agent 只开放文件与 shell（加调用方的 MCP）。"""

    toolsets: Sequence[AbstractToolset[RunScope]]
    include_mcp: bool


@dataclass(frozen=True)
class ToolGroup:
    """一组能力：若干工具集，以及是否使用对它可见的 MCP server。"""

    toolsets: Sequence[AbstractToolset[RunScope]] = ()
    uses_mcp: bool = False


@dataclass(frozen=True)
class ToolGroups:
    """agent 按名称选用的工具组。always 里的（自我改进：私有 skill 与 memory）对每个 agent 都开放。

    用途是限制处理不可信输入的 agent 能做什么（例如处理外部事件的 agent 只开放通知）。
    """

    groups: Mapping[str, ToolGroup]
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
        return RunProfile(
            toolsets=[*self.always, *(t for g in chosen for t in g.toolsets)],
            include_mcp=any(g.uses_mcp for g in chosen),
        )


class AgentRuntime:
    def __init__(
        self,
        *,
        agents: AgentService,
        skills: SkillStore,
        memory: MemoryStore,
        prompts: PromptStore,
        mcp: McpRegistry,
        live: LiveRuns,
        tz: ZoneInfo,
        clock: Clock = utcnow,
        resolve_model: ModelResolver,
        files: FileStore,
    ):
        self._agents = agents
        self._skills = skills
        self._memory = memory
        self._prompts = prompts
        self._mcp = mcp
        self._live = live
        self._tz = tz
        self._clock = clock
        self._resolve_model = resolve_model
        self._pai = PaiAgent(deps_type=RunScope, output_type=str, capabilities=[AttachmentsForModel(files)])

    def workspace(self, agent_id: str) -> Path:
        return self._agents.workspace(agent_id)

    def scope(
        self, agent: Agent, *, session_id: str | None, origin: Origin | None, reply_to: ReplyTo | None
    ) -> RunScope:
        return RunScope(
            agent_id=agent.id,
            workspace=self.workspace(agent.id),
            session_id=session_id,
            origin=origin,
            reply_to=reply_to,
        )

    async def instructions(self, agent: Agent, scope: RunScope, summary: str | None) -> str:
        return build_instructions(
            agent_name=agent.name,
            agent_description=agent.description,
            public_instructions=self._prompts.current(None),
            agent_instructions=self._prompts.current(agent.id),
            workspace=scope.workspace,
            now_local=self._clock().astimezone(self._tz),
            origin=scope.origin,
            reply_to=scope.reply_to,
            memory=self._memory.read(agent.id),
            skills=self._skills.visible_to(agent.id),
            memory_limit=self._memory.max_chars,
            summary=summary,
        )

    async def run_steps(
        self,
        agent: Agent,
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
        在那里并入新信件、保存这一步。可以停在挂起的工具调用上（ask_user，见 runtime.questions）：输出为
        DeferredToolRequests；之后带着 deferred（用户的回答）再运行就从那里继续。

        prompt 为 None 时从历史续跑：历史停在一条请求上就重发它，停在带工具调用的回复上就执行这些调用（pydantic-ai
        的续跑语义）。"""
        model = agent.model
        ctx = {
            "agent": scope.agent_id,
            "session_id": scope.session_id,
            "model": model,
            "workspace": str(scope.workspace),
        }
        log.info("agent run started", extra={**ctx, "history": len(history)})
        async with self._pai.iter(
            None if prompt is None else list(prompt),
            model=self._resolve_model(model),
            instructions=await self.instructions(agent, scope, summary),
            message_history=list(history),
            deps=scope,
            toolsets=await self._toolsets(agent.id, profile),
            output_type=[str, DeferredToolRequests],
            deferred_tool_results=deferred,
        ) as run:
            node = run.next_node
            if not PaiAgent.is_user_prompt_node(node):
                raise RuntimeError(f"agent run did not start with the user prompt node: {node!r}")
            node = await run.next(node)
            await hooks.started(run.ctx.state.message_history)
            while not isinstance(node, End):
                if PaiAgent.is_model_request_node(node):
                    await hooks.before_request(node.request, run.ctx.state.message_history)
                    async with node.stream(run.ctx) as stream:
                        await self._forward_events(scope, stream)
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
                        await self._forward_events(scope, stream)
                    node = await run.next(node)
                else:
                    node = await run.next(node)
            result = run.result
        if result is None:
            raise RuntimeError("agent run ended without a result")
        usage = result.usage
        log.info(
            "agent run finished",
            extra={
                **ctx,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "requests": usage.requests,
            },
        )
        return result

    async def run_once(
        self, agent: Agent, scope: RunScope, prompt: str, profile: RunProfile
    ) -> AgentRunResult[str]:
        """agent 一次不属于会话的运行（反思）：带它完整的指令，没有历史，不逐步保存。"""
        return await self._execute(
            model=agent.model,
            instructions=await self.instructions(agent, scope, None),
            toolsets=await self._toolsets(agent.id, profile),
            scope=scope,
            prompt=prompt,
            history=(),
        )

    async def run_temp(
        self,
        parent: Agent,
        *,
        task: str,
        instructions: str,
        model: str,
        profile: RunProfile,
        workspace: Path,
        request_limit: int,
    ) -> Answer:
        """一次性的临时 agent：替 parent 做一件事，返回最终回答，以及它用工具挂给用户的文件（如 share_file）。

        没有记忆、skill、历史与反思；工具来自 profile（不含委派与再开临时 agent），MCP 与 secret 按 parent 的视角。
        也不带公共 prompt：公共 prompt 约定的是长期 agent 之间的共同规则，临时 agent 只执行调用方交给它的任务。
        不属于任何会话，运行事件不推到会话页。模型请求数超过 request_limit 时抛出 UsageLimitExceeded。
        """
        scope = RunScope(agent_id=parent.id, workspace=workspace, session_id=None, origin=None, reply_to=None)
        result = await self._execute(
            model=model,
            instructions=build_temp_instructions(
                parent_name=parent.name,
                workspace=workspace,
                now_local=self._clock().astimezone(self._tz),
                extra=instructions,
            ),
            toolsets=await self._toolsets(parent.id, profile),
            scope=scope,
            prompt=task,
            history=(),
            usage_limits=UsageLimits(request_limit=request_limit),
        )
        return Answer.of_run(result.output, result.new_messages())

    async def _toolsets(self, agent_id: str, profile: RunProfile) -> list[AbstractToolset[RunScope]]:
        toolsets: list[AbstractToolset[RunScope]] = [DomainErrorsAsRetry(t) for t in profile.toolsets]
        if profile.include_mcp:
            toolsets += [await self._mcp.build_toolset(s) for s in self._mcp.visible_to(agent_id)]
        return toolsets

    async def _execute(
        self,
        *,
        model: str,
        instructions: str,
        toolsets: list[AbstractToolset[RunScope]],
        scope: RunScope,
        prompt: str,
        history: Sequence[ModelMessage],
        usage_limits: UsageLimits | None = None,
    ) -> AgentRunResult[str]:
        ctx = {
            "agent": scope.agent_id,
            "session_id": scope.session_id,
            "model": model,
            "workspace": str(scope.workspace),
        }
        log.info("agent run started", extra={**ctx, "history": len(history)})
        result = await self._pai.run(
            prompt,
            model=self._resolve_model(model),
            instructions=instructions,
            message_history=list(history),
            deps=scope,
            toolsets=toolsets,
            event_stream_handler=lambda run_ctx, stream: self._forward_events(run_ctx.deps, stream),
            usage_limits=usage_limits,
        )
        usage = result.usage
        log.info(
            "agent run finished",
            extra={
                **ctx,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "requests": usage.requests,
            },
        )
        return result

    async def summarize(self, agent: Agent, instructions: str, prompt: str) -> str:
        """用该 agent 的模型做一次无工具的文本生成（上下文压缩用）。不转发事件到前端。"""
        result = await self._pai.run(
            prompt, model=self._resolve_model(agent.model), instructions=instructions
        )
        return result.output

    async def _forward_events(self, scope: RunScope, stream: AsyncIterable[AgentStreamEvent]) -> None:
        base = {"type": "run", "session_id": scope.session_id, "agent": scope.agent_id}
        async for event in stream:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart) and event.part.content:
                self._live.publish(scope.session_id, {**base, "event": "text", "text": event.part.content})
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                self._live.publish(
                    scope.session_id, {**base, "event": "text", "text": event.delta.content_delta}
                )
            elif (
                isinstance(event, PartStartEvent)
                and isinstance(event.part, ThinkingPart)
                and event.part.content
            ):
                self._live.publish(
                    scope.session_id, {**base, "event": "thinking", "text": event.part.content}
                )
            # 只更新签名、没有新内容的增量（content_delta 为 None）不用展示
            elif (
                isinstance(event, PartDeltaEvent)
                and isinstance(event.delta, ThinkingPartDelta)
                and event.delta.content_delta
            ):
                self._live.publish(
                    scope.session_id, {**base, "event": "thinking", "text": event.delta.content_delta}
                )
            elif isinstance(event, FunctionToolCallEvent):
                call = redact_call(event.part)
                self._live.publish(
                    scope.session_id,
                    {
                        **base,
                        "event": "tool_call",
                        "tool_name": call.tool_name,
                        "tool_call_id": call.tool_call_id,
                        "text": truncate(call.args_as_json_str(), _EVENT_TEXT_LIMIT),
                        "attachments": [],
                        "threads": [],
                    },
                )
            elif isinstance(event, FunctionToolResultEvent):
                part = event.part
                text = (
                    part.model_response_str() if isinstance(part, ToolReturnPart) else part.model_response()
                )
                self._live.publish(
                    scope.session_id,
                    {
                        **base,
                        "event": "tool_result" if isinstance(part, ToolReturnPart) else "tool_error",
                        "tool_name": part.tool_name,
                        "tool_call_id": part.tool_call_id,
                        "text": truncate(text, _EVENT_TEXT_LIMIT),
                        "attachments": tool_attachments(part),
                        "threads": tool_threads(part),
                    },
                )
