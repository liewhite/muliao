"""执行一次 agent 运行：组装 instructions、toolsets、模型，转发流式事件到总线。"""

import logging
from collections.abc import AsyncIterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_ai import Agent as PaiAgent
from pydantic_ai import AgentRunResult, RunContext, UsageLimits
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolReturnPart,
)
from pydantic_ai.toolsets import AbstractToolset

from muliao.agents import AgentService
from muliao.bus import EventBus
from muliao.db import utcnow
from muliao.evolution.memory import MemoryStore
from muliao.evolution.prompts import PromptStore
from muliao.evolution.skills import SkillStore
from muliao.llm import ModelResolver
from muliao.models import Agent, Origin, ReplyTo
from muliao.runtime.instructions import build_instructions, build_temp_instructions
from muliao.runtime.mcp import McpRegistry
from muliao.runtime.redaction import redact_call
from muliao.runtime.scope import RunScope
from muliao.runtime.tasks import Clock
from muliao.runtime.tools.errors import DomainErrorsAsRetry
from muliao.transcript import truncate

log = logging.getLogger(__name__)

_EVENT_TEXT_LIMIT = 2000


@dataclass(frozen=True)
class RunProfile:
    """一类运行开放的能力：turn 全部开放，反思只开放 skill 与 memory，临时 agent 只开放文件与 shell（加调用方的 MCP）。"""

    toolsets: Sequence[AbstractToolset[RunScope]]
    include_mcp: bool


class AgentRuntime:
    def __init__(
        self,
        *,
        agents: AgentService,
        skills: SkillStore,
        memory: MemoryStore,
        prompts: PromptStore,
        mcp: McpRegistry,
        bus: EventBus,
        tz: ZoneInfo,
        clock: Clock = utcnow,
        resolve_model: ModelResolver,
    ):
        self._agents = agents
        self._skills = skills
        self._memory = memory
        self._prompts = prompts
        self._mcp = mcp
        self._bus = bus
        self._tz = tz
        self._clock = clock
        self._resolve_model = resolve_model
        self._pai = PaiAgent(deps_type=RunScope, output_type=str)

    def scope(
        self, agent: Agent, *, session_id: str | None, origin: Origin | None, reply_to: ReplyTo | None
    ) -> RunScope:
        return RunScope(
            agent_id=agent.id,
            workspace=self._agents.workspace(agent.id),
            session_id=session_id,
            origin=origin,
            reply_to=reply_to,
        )

    async def instructions(self, agent: Agent, scope: RunScope, summary: str | None) -> str:
        return build_instructions(
            agent_name=agent.name,
            agent_description=agent.description,
            public_instructions=await self._prompts.current(None),
            agent_instructions=await self._prompts.current(agent.id),
            workspace=scope.workspace,
            now_local=self._clock().astimezone(self._tz),
            origin=scope.origin,
            reply_to=scope.reply_to,
            memory=self._memory.read(agent.id),
            skills=self._skills.visible_to(agent.id),
            memory_limit=self._memory.max_chars,
            summary=summary,
        )

    async def run(
        self,
        agent: Agent,
        scope: RunScope,
        prompt: str,
        history: Sequence[ModelMessage],
        profile: RunProfile,
        *,
        summary: str | None = None,
    ) -> AgentRunResult[str]:
        return await self._execute(
            model=agent.model,
            instructions=await self.instructions(agent, scope, summary),
            toolsets=await self._toolsets(agent.id, profile),
            scope=scope,
            prompt=prompt,
            history=history,
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
    ) -> str:
        """一次性的临时 agent：替 parent 做一件事，返回最终回答。

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
        return result.output

    async def _toolsets(self, agent_id: str, profile: RunProfile) -> list[AbstractToolset[RunScope]]:
        toolsets: list[AbstractToolset[RunScope]] = [DomainErrorsAsRetry(t) for t in profile.toolsets]
        if profile.include_mcp:
            toolsets += [await self._mcp.build_toolset(s) for s in await self._mcp.visible_to(agent_id)]
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
            event_stream_handler=self._forward_events,
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
        self, ctx: RunContext[RunScope], stream: AsyncIterable[AgentStreamEvent]
    ) -> None:
        base = {"type": "run", "session_id": ctx.deps.session_id, "agent": ctx.deps.agent_id}
        async for event in stream:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart) and event.part.content:
                self._bus.publish({**base, "event": "text", "text": event.part.content})
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                self._bus.publish({**base, "event": "text", "text": event.delta.content_delta})
            elif isinstance(event, FunctionToolCallEvent):
                call = redact_call(event.part)
                self._bus.publish(
                    {
                        **base,
                        "event": "tool_call",
                        "tool_name": call.tool_name,
                        "tool_call_id": call.tool_call_id,
                        "text": truncate(call.args_as_json_str(), _EVENT_TEXT_LIMIT),
                    }
                )
            elif isinstance(event, FunctionToolResultEvent):
                part = event.part
                text = (
                    part.model_response_str() if isinstance(part, ToolReturnPart) else part.model_response()
                )
                self._bus.publish(
                    {
                        **base,
                        "event": "tool_result" if isinstance(part, ToolReturnPart) else "tool_error",
                        "tool_name": part.tool_name,
                        "tool_call_id": part.tool_call_id,
                        "text": truncate(text, _EVENT_TEXT_LIMIT),
                    }
                )
