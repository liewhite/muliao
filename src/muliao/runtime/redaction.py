"""工具调用参数中的 secret 脱敏：在消息落库、推送到前端之前执行。

只能防止明文进入持久化的对话记录；同一轮运行中模型已经看到过明文，用户 prompt 里的明文也无法保护。
"""

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

# 工具名 → 需要脱敏的参数名
SECRET_TOOL_ARGS: dict[str, frozenset[str]] = {"vault_set": frozenset({"value"})}
REDACTED = "[redacted]"


def redact_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    secret = SECRET_TOOL_ARGS.get(tool_name)
    if not secret:
        return args
    return {k: REDACTED if k in secret else v for k, v in args.items()}


def redact_call(part: ToolCallPart) -> ToolCallPart:
    if part.tool_name not in SECRET_TOOL_ARGS:
        return part
    redacted = redact_args(part.tool_name, part.args_as_dict())
    args: str | dict[str, Any] = json.dumps(redacted) if isinstance(part.args, str) else redacted
    return dataclasses.replace(part, args=args)


def redact_messages(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    result: list[ModelMessage] = []
    for msg in messages:
        if isinstance(msg, ModelResponse) and any(
            isinstance(p, ToolCallPart) and p.tool_name in SECRET_TOOL_ARGS for p in msg.parts
        ):
            msg = dataclasses.replace(
                msg, parts=[redact_call(p) if isinstance(p, ToolCallPart) else p for p in msg.parts]
            )
        result.append(msg)
    return result
