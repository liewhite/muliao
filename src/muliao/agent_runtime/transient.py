"""模型提供方与网络层的暂时性失败：哪些算暂时性的是 LLM SDK 与传输层的知识，由 agent_runtime 判断。一次模型运行
（AgentRuntime 的每个入口）里抛出的暂时性失败在这里换成 ModelUnavailable（ServiceUnavailable 的一种，按 outage 策略重试），
任务队列只认 TransientError 与 TimeoutError，不依赖任何 SDK（docs/architecture.md 第 5 节）。"""

from collections.abc import Iterator
from contextlib import contextmanager

import anthropic
import httpx
import httpx2
import openai
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from muliao.errors import ModelUnavailable, TransientError

# 模型提供方契约中声明的暂时性状态码
_TRANSIENT_HTTP = {408, 409, 425, 429, 500, 502, 503, 504, 529}

# 网络层的暂时性失败。pydantic-ai 把它们包装为 ModelAPIError，原始异常在 __cause__ 中
_TRANSPORT_ERRORS = (
    httpx.TransportError,
    httpx2.TransportError,
    anthropic.APIConnectionError,
    openai.APIConnectionError,
    TimeoutError,
)


def _broken_off_stream(cause: BaseException | None) -> bool:
    """流已以 200 开始、中途收到类型不在 Anthropic 契约里的 error 事件（如中转站上游空闲超时的 `stream_timeout`）。
    契约内的类型（overloaded_error、invalid_request_error 等）已由 pydantic-ai 按类型换成对应状态码的 ModelHTTPError，
    走到这里的只剩契约外的（含 data 不是 JSON 的）：请求已被接受、是流中途断开，与传输层断流同类。
    取舍：契约外类型一律当断流，不按中转站的具体类型特判；其中确定性的会在 outage 重试上限后失败，失败仍可见。
    OpenAI 的流内错误见 _openai_response_error。"""
    return isinstance(cause, anthropic.APIStatusError) and cause.status_code < 400


# OpenAI Responses 契约（ResponseError.code）里的服务端暂时性错误码，分别对应 5xx 与 429
_TRANSIENT_RESPONSE_CODES = {"server_error", "rate_limit_exceeded"}


def _openai_response_error(exc: ModelAPIError) -> bool:
    """OpenAI Responses 在 200 的响应体或流里报的失败（error / response.failed 事件）没有状态码：pydantic-ai 的
    `_response_error` 把它构造成无 cause 的 ModelAPIError，消息为 `<code>: <message>`，这是唯一带着错误码的地方。
    只认契约里的暂时性错误码；其余（invalid_prompt、图片错误等确定性的）与无错误码的不重试。"""
    code, sep, _ = exc.message.partition(": ")
    return exc.__cause__ is None and bool(sep) and code in _TRANSIENT_RESPONSE_CODES


def is_transient(exc: BaseException) -> bool:
    """契约内的暂时性失败才重试。ModelAPIError 还包括请求超限、响应解码失败等确定性错误，不能整类重试。"""
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in _TRANSIENT_HTTP
    if isinstance(exc, ModelAPIError):
        return (
            isinstance(exc.__cause__, _TRANSPORT_ERRORS)
            or _broken_off_stream(exc.__cause__)
            or _openai_response_error(exc)
        )
    return isinstance(exc, (TransientError, *_TRANSPORT_ERRORS))


@contextmanager
def transient_as_retryable() -> Iterator[None]:
    """把模型与网络层的暂时性失败换成 ModelUnavailable（原异常挂在 __cause__，信息保留类型与原文），其余原样抛出。
    已经是 TransientError 的（工具、MCP 等适配层抛的）保持原样，按普通暂时性失败重试。"""
    try:
        yield
    except TransientError:
        raise
    except Exception as e:
        if is_transient(e):
            raise ModelUnavailable(f"{type(e).__name__}: {e}") from e
        raise
