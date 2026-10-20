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
    UserContent,
)
from pydantic_ai.toolsets import AbstractToolset
from pydantic_graph import End

from muliao.agent_runtime.instructions import build_instructions, build_temp_instructions
from muliao.agent_runtime.llm import ModelResolver
from muliao.agent_runtime.redaction import redact_call
from muliao.agent_runtime.scope import Audience, RunScope
from muliao.agent_runtime.tool_errors import DomainErrorsAsRetry
from muliao.agents.memory import MemoryStore
from muliao.agents.prompts import PromptStore
from muliao.agents.service import AgentService
from muliao.agents.skills import SkillStore
from muliao.agents.tables import Agent
from muliao.files.store import FileStore
from muliao.platform.bus import LiveRuns
from muliao.platform.db import utcnow
from muliao.platform.tasks import Clock
from muliao.sessions.answers import Answer
from muliao.sessions.attachments import AttachmentsForModel
from muliao.sessions.tables import Origin
from muliao.sessions.transcript import Item, call_item, result_item, truncate

log = logging.getLogger(__name__)

_EVENT_TEXT_LIMIT = 2000


EventHandler = Callable[[RunContext[RunScope], AsyncIterable[AgentStreamEvent]], Awaitable[None]]


@dataclass(frozen=True)
class StepHooks:
    """逐步运行（AgentRuntime.run_steps）时消费者在每一步的两个点上介入（sessions.consumer）。

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
        self._live = live
        self._tz = tz
        self._clock = clock
        self._resolve_model = resolve_model
        self._pai = PaiAgent(deps_type=RunScope, output_type=str, capabilities=[AttachmentsForModel(files)])

    def workspace(self, agent_id: str) -> Path:
        return self._agents.workspace(agent_id)

    def scope(
        self, agent: Agent, *, session_id: str | None, origin: Origin | None, audience: Audience | None
    ) -> RunScope:
        return RunScope(
            agent_id=agent.id,
            workspace=self.workspace(agent.id),
            session_id=session_id,
            origin=origin,
            audience=audience,
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
            audience=scope.audience,
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
        log.info("agent run started", extra={**ctx, "history": len(history)})
        # 这次交回结果的挂起调用：调用已在历史里，结果随第一步保存
        resumed = frozenset(deferred.calls) if deferred is not None else frozenset()
        async with self._pai.iter(
            None if prompt is None else list(prompt),
            model=self._resolve_model(model),
            instructions=await self.instructions(agent, scope, summary),
            message_history=list(history),
            deps=scope,
            toolsets=self._toolsets(profile),
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
                        await self._forward_events(session_id, scope.agent_id, stream, resumed)
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
                        await self._forward_events(session_id, scope.agent_id, stream, resumed)
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
            toolsets=self._toolsets(profile),
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
        scope = RunScope(agent_id=parent.id, workspace=workspace, session_id=None, origin=None, audience=None)
        result = await self._execute(
            model=model,
            instructions=build_temp_instructions(
                parent_name=parent.name,
                workspace=workspace,
                now_local=self._clock().astimezone(self._tz),
                extra=instructions,
            ),
            toolsets=self._toolsets(profile),
            scope=scope,
            prompt=task,
            history=(),
            usage_limits=UsageLimits(request_limit=request_limit),
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

    async def _forward_events(
        self,
        session_id: str,
        agent_id: str,
        stream: AsyncIterable[AgentStreamEvent],
        resumed: frozenset[str],
    ) -> None:
        """会话运行的事件推到会话页（bus.LiveRuns），条目与历史同一个投影（transcript）。没有会话的运行（反思、临时
        agent）没有页面在看，不转发。工具的参数与结果在实时事件里截断（保存后历史里是完整的）。续跑时 pydantic-ai 会把
        交回结果的挂起调用（resumed）再发一遍调用与结果事件：它们已经在历史里，不转发。"""
        async for event in stream:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart) and event.part.content:
                self._live.publish_delta(session_id, agent_id, "assistant", event.part.content)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                self._live.publish_delta(session_id, agent_id, "assistant", event.delta.content_delta)
            elif (
                isinstance(event, PartStartEvent)
                and isinstance(event.part, ThinkingPart)
                and event.part.content
            ):
                self._live.publish_delta(session_id, agent_id, "thinking", event.part.content)
            # 只更新签名、没有新内容的增量（content_delta 为 None）不用展示
            elif (
                isinstance(event, PartDeltaEvent)
                and isinstance(event.delta, ThinkingPartDelta)
                and event.delta.content_delta
            ):
                self._live.publish_delta(session_id, agent_id, "thinking", event.delta.content_delta)
            elif isinstance(event, FunctionToolCallEvent | FunctionToolResultEvent) and (
                event.part.tool_call_id in resumed
            ):
                continue
            elif isinstance(event, FunctionToolCallEvent):
                self._live.publish_item(session_id, agent_id, _truncated(call_item(redact_call(event.part))))
            elif isinstance(event, FunctionToolResultEvent):
                self._live.publish_item(session_id, agent_id, _truncated(result_item(event.part)))


def _truncated(item: Item) -> Item:
    return item.model_copy(update={"text": truncate(item.text, _EVENT_TEXT_LIMIT)})
