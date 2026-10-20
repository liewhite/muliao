"""把「消息 → ModelResponse」的脚本函数包装成同时支持流式请求的 FunctionModel。"""

import json
from collections.abc import AsyncIterator, Callable

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from muliao.sessions.conventions import unanswered_calls

Script = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def scripted(fn: Script) -> FunctionModel:
    """包装脚本函数，并像真实提供方一样拒绝工具调用缺结果的请求（OpenAI、Anthropic 会返回 400）：function model
    本身不校验，这类缺陷在测试里会被放过。"""
    inner = fn

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if dangling := unanswered_calls(messages):
            raise AssertionError(
                f"model request has tool calls without results: {[c.tool_call_id for c in dangling]}"
            )
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

    return FunctionModel(fn, stream_function=stream)
