"""工具调用参数中的 secret 脱敏：在消息落库、推送到前端之前执行。

只能防止明文进入持久化的对话记录；同一轮运行中模型已经看到过明文，用户 prompt 里的明文也无法保护。
"""

import dataclasses
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from pydantic import ValidationError
from pydantic_ai.exceptions import ToolRetryError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart

# SECRET_TOOL_ARGS（工具名 → 需要脱敏的参数名）由定义这些工具的 agents 领域声明（agents/secret_tools.py）
from muliao.agents.api import SECRET_TOOL_ARGS

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


def redact_retry(part: RetryPromptPart) -> RetryPromptPart:
    """参数校验失败时交还模型的错误：pydantic 的每条错误都带着 input（出错的那部分参数，缺字段时是整个参数
    dict），里面就有明文。去掉 input，错误位置与原因（loc、msg、type）照常留给模型看。工具自己报的错误（字符串）
    由工具负责不带明文。"""
    if part.tool_name not in SECRET_TOOL_ARGS or isinstance(part.content, str):
        return part
    return dataclasses.replace(part, content=[{**e, "input": REDACTED} for e in part.content])


@contextmanager
def tool_input_kept_out_of_failures() -> Iterator[None]:
    """一个工具的重试次数用完时，pydantic-ai 抛 UnexpectedModelBehavior，原因挂着最后一次的校验错误（ValidationError
    的 str 里有 input_value，即参数原文）或 ToolRetryError（带 RetryPromptPart）。运行失败会连同 traceback 进服务
    日志，所以这里换成不带原因的同一异常：每次重试的错误已经（脱敏后）存在会话历史里，日志只需说明哪个工具用完了
    重试。所有工具一样处理，不按工具名区分（原因里只有 ToolRetryError 带工具名）。"""
    try:
        yield
    except UnexpectedModelBehavior as e:
        if not isinstance(e.__cause__, ValidationError | ToolRetryError):
            raise
        raise UnexpectedModelBehavior(e.message, e.body) from None


def redact_messages(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    result: list[ModelMessage] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            msg = dataclasses.replace(
                msg, parts=[redact_call(p) if isinstance(p, ToolCallPart) else p for p in msg.parts]
            )
        elif isinstance(msg, ModelRequest):
            msg = dataclasses.replace(
                msg, parts=[redact_retry(p) if isinstance(p, RetryPromptPart) else p for p in msg.parts]
            )
        result.append(msg)
    return result
