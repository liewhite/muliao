"""网页搜索与抓取。`WebProvider` 是 web 工具组依赖的唯一接口，具体服务是它的实现，由配置 `[web]` 的 type 选择。

各服务自己的输入限制（query 长度、条数上限、URL 规则）以服务端校验为唯一来源，这里不重复一份：
服务端拒绝时把它的原因作为 InvalidInput 交还模型修正。
"""

import logging
from dataclasses import dataclass
from typing import Literal, Protocol, assert_never

import anyio
import httpx
from pydantic import BaseModel, ValidationError

from muliao.config import WebConfig
from muliao.errors import InvalidInput, TransientError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    # 给模型读的摘要
    summary: str
    site_name: str | None
    # 发布时间（RFC 3339），未知时为 None
    published: str | None


@dataclass(frozen=True)
class Page:
    title: str
    # Markdown 正文；直连抓取时是原样的纯文本（见 _direct_fetch）
    content: str


class WebProvider(Protocol):
    async def search(self, query: str, count: int) -> list[SearchHit]: ...

    async def fetch(self, url: str) -> Page: ...


# --- DeepSeek：https://api-docs.deepseek.com/zh-cn/guides/search ---

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# 抓取要等对方站点，给足时间；挂起时及时报错，而不是一直占着这次运行
_DEEPSEEK_TIMEOUT_SECONDS = 60.0
# 文档：408、429、5xx 等待后重试；例外：fetch 带错误体的 408/504 是目标站点超时，见 _DEEPSEEK_REJECTED_INPUT
_DEEPSEEK_TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})

_Op = Literal["search", "fetch"]
# 由模型给出的输入引起、按契约交还模型修正的状态码，按接口区分：400 字段值无效、413 请求体过大；
# fetch 另有 403 被安全策略拒绝、404 抓取不到可用内容，以及带契约错误体的 408/504：是目标站点抓取超时
# （如 "Request timeout"），重试同一 URL 多半还是超时，交还模型决定换 URL 还是放弃（#134）。
# 其余（401 key 无效、402 余额不足、search 的 403/404……）
# 是配置、账户问题或契约之外的响应，模型改不了，让这一轮失败可见
_DEEPSEEK_REJECTED_INPUT: dict[_Op, frozenset[int]] = {
    "search": frozenset({400, 413}),
    "fetch": frozenset({400, 403, 404, 408, 413, 504}),
}


class _DeepSeekResult(BaseModel):
    title: str
    url: str
    summary: str
    site_name: str | None
    date: str | None


class _DeepSeekSearchResponse(BaseModel):
    results: list[_DeepSeekResult]


class _DeepSeekFetchResponse(BaseModel):
    title: str
    content: str


class _DeepSeekErrorBody(BaseModel):
    message: str


class _DeepSeekError(BaseModel):
    error: _DeepSeekErrorBody


def _error_message(resp: httpx.Response) -> str | None:
    """文档约定的错误体 {"error": {"message": ...}} 里的原因；不是这个格式时（如网关返回的 502 页面、
    base_url 指错时别的服务的 404）返回 None：这样的响应不是 DeepSeek 按契约给出的错误。"""
    try:
        return _DeepSeekError.model_validate_json(resp.content).error.message
    except ValidationError:
        return None


class _FetchNotFound(InvalidInput):
    """fetch 按契约返回 404（"failed to fetch URL"）：服务没拿到可用内容。它既可能是页面不存在，也可能是服务自己
    抓不到能直连访问的地址（实测 raw.githubusercontent.com 的纯文本文件都是这样，#189），由 `DeepSeekWeb.fetch` 直连核实。"""


class DeepSeekWeb:
    def __init__(self, api_key: str, base_url: str, client: httpx.AsyncClient):
        # 共用的出站 HTTP 客户端，由容器创建、在关停时关闭
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def _post(self, op: _Op, body: dict[str, object]) -> object:
        try:
            resp = await self._client.post(
                f"{self._base_url}/v1/tools/{op}",
                json=body,
                headers=self._headers,
                timeout=_DEEPSEEK_TIMEOUT_SECONDS,
            )
        except httpx.TransportError as e:
            # 连不上、读超时也是这个适配层自己的暂时性失败：自己换成 TransientError，否则冒到运行边界会被当作模型
            # 不可用（ModelUnavailable），按长得多的 outage 重试上限重试
            reason = f"deepseek {op} failed: {type(e).__name__}: {e}"
            log.warning("web request failed", extra={"provider": "deepseek", "op": op, "reason": reason})
            raise TransientError(reason) from e
        if resp.status_code == 200:
            return resp.json()
        message = _error_message(resp)
        reason = f"deepseek {op} failed: HTTP {resp.status_code}: " + (
            message if message is not None else f"response outside the error contract: {resp.text[:500]}"
        )
        log.warning("web request failed", extra={"provider": "deepseek", "op": op, "reason": reason})
        # 只有服务按契约拒绝的输入才交还模型；状态码对上而错误体不对是网关或配置问题（如网关自己的 504 页面、
        # base_url 指错），按下面的暂时性/配置失败处理
        if resp.status_code in _DEEPSEEK_REJECTED_INPUT[op] and message is not None:
            if op == "fetch" and resp.status_code == 404:
                raise _FetchNotFound(reason)
            raise InvalidInput(reason)
        if resp.status_code in _DEEPSEEK_TRANSIENT:
            raise TransientError(reason)
        raise RuntimeError(reason)

    async def search(self, query: str, count: int) -> list[SearchHit]:
        raw = await self._post("search", {"query": query, "count": count})
        results = _DeepSeekSearchResponse.model_validate(raw).results
        log.info("web search", extra={"provider": "deepseek", "count": len(results)})
        return [
            SearchHit(title=r.title, url=r.url, summary=r.summary, site_name=r.site_name, published=r.date)
            for r in results
        ]

    async def fetch(self, url: str) -> Page:
        try:
            raw = await self._post("fetch", {"url": url})
        except _FetchNotFound as e:
            return await _direct_fetch(self._client, url, str(e))
        page = _DeepSeekFetchResponse.model_validate(raw)
        # 只记 host：URL 里可能带着对话内容
        host = httpx.URL(url).host
        log.info("web fetch", extra={"provider": "deepseek", "host": host, "chars": len(page.content)})
        return Page(title=page.title, content=page.content)


# --- 直连抓取：抓取服务拿不到时核实一次 ---

_DIRECT_TIMEOUT_SECONDS = 20.0
# 读到这么多就停：正文最终还会按 tool_output_max_chars 截断，这里只防下载大文件
_DIRECT_MAX_BYTES = 2 * 1024 * 1024
_DIRECT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; muliao web_fetch)"}
# 不经转换就能直接给模型读的非 text/* 类型；HTML 不在内：转成 Markdown 是抓取服务的事，这里不做
_DIRECT_TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
    }
)


def _is_plain_text(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    if media in ("text/html", "application/xhtml+xml"):
        return False
    return media.startswith("text/") or media in _DIRECT_TEXT_TYPES or media.endswith(("+json", "+xml"))


class _DirectFailed(Exception):
    """直连没拿到可直接返回的纯文本；消息是给模型看的原因。"""


async def _direct_get(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """GET 一次，返回 (content-type, 正文)；整段（连接到读完）限时 _DIRECT_TIMEOUT_SECONDS。"""
    try:
        with anyio.fail_after(_DIRECT_TIMEOUT_SECONDS):
            async with client.stream("GET", url, headers=_DIRECT_HEADERS, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    raise _DirectFailed(f"a direct GET returned HTTP {resp.status_code}")
                content_type = resp.headers.get("content-type", "")
                if not _is_plain_text(content_type):
                    raise _DirectFailed(
                        f"a direct GET returned content-type {content_type or '(none)'}, which web_fetch does not "
                        "convert: try `run_shell` with curl"
                    )
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) >= _DIRECT_MAX_BYTES:
                        break
                # 流式响应的 encoding 没有 charset 时取客户端的默认值（utf-8），不会是 None
                return content_type, bytes(body[:_DIRECT_MAX_BYTES]).decode(resp.encoding, errors="replace")
    except TimeoutError as e:
        raise _DirectFailed(f"a direct GET did not finish within {_DIRECT_TIMEOUT_SECONDS:g}s") from e
    except httpx.HTTPError as e:
        raise _DirectFailed(f"a direct GET failed: {type(e).__name__}: {e}") from e


async def _direct_fetch(client: httpx.AsyncClient, url: str, service_reason: str) -> Page:
    """抓取服务说拿不到（`service_reason`）时直接 GET 一次：拿到纯文本（源码、Markdown、JSON……）就原样返回并注明是直连；
    否则把两边的原因一起交还模型（InvalidInput）。直连 404 才说明页面确实不存在，所以服务那句 404 不能单独交还（#189）。
    直连失败不算暂时性失败：抓取服务已经给了确定答复，连不上的是目标站点，与 #134 的目标站点超时同理交还模型。"""
    # 只记 host：URL 里可能带着对话内容
    host = httpx.URL(url).host
    try:
        content_type, text = await _direct_get(client, url)
    except _DirectFailed as e:
        log.warning(
            "web request failed", extra={"provider": "direct", "op": "fetch", "host": host, "reason": str(e)}
        )
        raise InvalidInput(
            f"{service_reason}. The fetch service could not get this URL, which does not by itself mean the page is "
            f"missing; {e}"
        ) from e
    log.info("web fetch", extra={"provider": "direct", "host": host, "chars": len(text)})
    note = f"(Fetched directly with a plain GET because the fetch service could not get it; content-type {content_type}.)"
    return Page(title="", content=f"{note}\n\n{text}")


def build_web_provider(cfg: WebConfig, client: httpx.AsyncClient) -> WebProvider:
    match cfg.type:
        case "deepseek":
            # 与 LLM provider 一样，地址只来自配置，未填时显式使用官方地址
            return DeepSeekWeb(cfg.api_key.get_secret_value(), cfg.base_url or DEEPSEEK_BASE_URL, client)
        case _:
            assert_never(cfg.type)
