"""工具调用参数中的 secret 脱敏：在消息落库、推送到前端之前执行。

只能防止明文进入持久化的对话记录；同一轮运行中模型已经看到过明文，用户 prompt 里的明文也无法保护。
"""

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

# SECRET_TOOL_ARGS（工具名 → 需要脱敏的参数名）由定义这些工具的 vault 领域声明
from muliao.vault.api import SECRET_TOOL_ARGS

REDACTED = "[redacted]"


def redact_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    secret = SECRET_TOOL_ARGS.get(tool_name)
    if not secret:
        return args
    return {k: REDACTED if k in secret else v for k, v in args.items()}


def _args_dict(part: ToolCallPart) -> dict[str, Any] | None:
    """参数解析成 dict；不是合法的 JSON 对象时为 None（模型写错，或用户停止时参数还没生成完被截断）。

    不用 args_as_dict：pydantic-ai 2.57 遇到非法 JSON 不报错，而是返回 {"INVALID_JSON": 原文}，原文里就有明文。
    """
    if isinstance(part.args, dict):
        return part.args
    if not part.args:
        return {}
    try:
        parsed = json.loads(part.args)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def redact_call(part: ToolCallPart) -> ToolCallPart:
    if part.tool_name not in SECRET_TOOL_ARGS:
        return part
    parsed = _args_dict(part)
    if parsed is None:
        # 无法定位字段：整体替换，宁可丢掉参数也不留下明文
        return dataclasses.replace(part, args=json.dumps({"_": REDACTED}))
    redacted = redact_args(part.tool_name, parsed)
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
