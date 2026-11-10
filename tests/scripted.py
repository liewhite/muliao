"""把「消息 → ModelResponse」的脚本函数包装成同时支持流式请求的 FunctionModel。"""

import json
from collections.abc import AsyncIterator, Callable

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.profiles import ModelProfile

from muliao.sessions.conventions import calls_in, results_in, unanswered_calls

Script = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def scripted(fn: Script, profile: ModelProfile | None = None) -> FunctionModel:
    """包装脚本函数，并像真实提供方一样拒绝工具调用缺结果、或结果不紧跟在它的调用之后的请求（OpenAI、Anthropic 会
    返回 400）：function model 本身不校验，这类缺陷在测试里会被放过。"""
    inner = fn

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if dangling := unanswered_calls(messages):
            raise AssertionError(
                f"model request has tool calls without results: {[c.tool_call_id for c in dangling]}"
            )
        if misplaced := _misplaced_results(messages):
            raise AssertionError(f"model request has tool results not right after their calls: {misplaced}")
        return inner(messages, info)

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        response = fn(messages, info)
        for i, part in enumerate(response.parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                args = part.args if isinstance(part.args, str) else json.dumps(part.args or {})
                yield {i: DeltaToolCall(name=part.tool_name, json_args=args, tool_call_id=part.tool_call_id)}
            else:
                raise TypeError(f"unsupported scripted part {part!r}")

    return FunctionModel(fn, stream_function=stream, profile=profile)


def _misplaced_results(messages: list[ModelMessage]) -> list[str]:
    """结果所在的请求前面那条不是发出这个调用的回复（提供方要求结果紧跟在调用之后）。不带工具名的 RetryPromptPart
    是对整条回复的重试提示（如「请返回文字或调用工具」），不针对调用。"""
    misplaced: list[str] = []
    # 发给模型的请求已由 pydantic-ai 合并相邻的，每条请求的前一条就是一条回复
    previous: set[str] = set()
    for m in messages:
        if isinstance(m, ModelResponse):
            previous = {c.tool_call_id for c in calls_in(m)}
            continue
        named = {p.tool_call_id for p in m.parts if getattr(p, "tool_name", None)}
        misplaced += sorted((results_in(m) & named) - previous)
    return misplaced
