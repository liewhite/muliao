"""把「消息 → ModelResponse」的脚本函数包装成同时支持流式请求的 FunctionModel。"""

import json
from collections.abc import AsyncIterator, Callable

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

Script = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def scripted(fn: Script) -> FunctionModel:
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
