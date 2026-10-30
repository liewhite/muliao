"""网页搜索与抓取。`WebProvider` 是 web 工具组依赖的唯一接口，具体服务是它的实现，由配置 `[web]` 的 type 选择。

各服务自己的输入限制（query 长度、条数上限、URL 规则）以服务端校验为唯一来源，这里不重复一份：
服务端拒绝时把它的原因作为 InvalidInput 交还模型修正。
"""

import logging
from dataclasses import dataclass
from typing import Literal, Protocol, assert_never

import anyio
import httpx
import trafilatura
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
    # Markdown 正文；直连抓到纯文本时是原文（见 _direct_fetch）
    content: str


class WebProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[SearchHit]: ...

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
# 其中说的是「目标抓不到」而不是输入无效的那些，改为直连核实（见 _FetchUnavailable）
_FETCH_UNAVAILABLE = frozenset({403, 404, 408, 504})


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


class _FetchUnavailable(InvalidInput):
    """fetch 按契约说目标抓不到：404（"failed to fetch URL"）、403（安全策略拒绝）、408/504（目标站点超时）。
    抓取服务在国内，境外站点常被墙或超时，它拿不到不说明页面不存在（实测 raw.githubusercontent.com 的纯文本文件
    都是 404，#189），由 `DeepSeekWeb.fetch` 直连核实（#309）。"""


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
            if op == "fetch" and resp.status_code in _FETCH_UNAVAILABLE:
                raise _FetchUnavailable(reason)
            raise InvalidInput(reason)
        if resp.status_code in _DEEPSEEK_TRANSIENT:
            raise TransientError(reason)
        raise RuntimeError(reason)

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        raw = await self._post("search", {"query": query, "count": limit})
        results = _DeepSeekSearchResponse.model_validate(raw).results
        log.info("web search", extra={"provider": "deepseek", "count": len(results)})
        return [
            SearchHit(title=r.title, url=r.url, summary=r.summary, site_name=r.site_name, published=r.date)
            for r in results
        ]

    async def fetch(self, url: str) -> Page:
        try:
            raw = await self._post("fetch", {"url": url})
        except (_FetchUnavailable, TransientError) as e:
            # 服务说目标抓不到，或服务本身暂时不可用：都直连一次作备用（#309）。
            # 400/413（URL 本身无效）与 key、余额等配置问题不在此列，照旧交还模型或让运行失败
            return await _direct_fetch(self._client, url, str(e))
        page = _DeepSeekFetchResponse.model_validate(raw)
        # 只记 host：URL 里可能带着对话内容
        host = httpx.URL(url).host
        log.info("web fetch", extra={"provider": "deepseek", "host": host, "chars": len(page.content)})
        return Page(title=page.title, content=page.content)


# --- 直连抓取：抓取服务拿不到或不可用时的备用（#189、#309） ---

_DIRECT_TIMEOUT_SECONDS = 20.0
# 读到这么多就停：正文最终还会按 tool_output_max_chars 截断，这里只防下载大文件
_DIRECT_MAX_BYTES = 2 * 1024 * 1024
_DIRECT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; muliao web_fetch)"}
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# 不经转换就能直接给模型读的非 text/* 类型
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


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_readable(media: str) -> bool:
    """HTML（本地转成 Markdown）或纯文本（原样返回）。"""
    return (
        media in _HTML_TYPES
        or media.startswith("text/")
        or media in _DIRECT_TEXT_TYPES
        or media.endswith(("+json", "+xml"))
    )


class _DirectFailed(Exception):
    """直连没拿到能给模型读的内容；消息是给模型看的原因。"""


@dataclass(frozen=True)
class _Response:
    # 跟随重定向后的最终地址：HTML 里的相对链接相对它
    url: str
    content_type: str
    body: bytes
    # 响应头的 charset；没有时是客户端默认值（utf-8）
    encoding: str


async def _direct_get(client: httpx.AsyncClient, url: str) -> _Response:
    """GET 一次；整段（连接到读完）限时 _DIRECT_TIMEOUT_SECONDS，最多读 _DIRECT_MAX_BYTES。"""
    try:
        with anyio.fail_after(_DIRECT_TIMEOUT_SECONDS):
            async with client.stream("GET", url, headers=_DIRECT_HEADERS, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    raise _DirectFailed(f"a direct GET returned HTTP {resp.status_code}")
                content_type = resp.headers.get("content-type", "")
                if not _is_readable(_media_type(content_type)):
                    raise _DirectFailed(
                        f"a direct GET returned content-type {content_type or '(none)'}, which web_fetch cannot "
                        "read: try `run_shell` with curl"
                    )
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) >= _DIRECT_MAX_BYTES:
                        break
                return _Response(
                    url=str(resp.url),
                    content_type=content_type,
                    body=bytes(body[:_DIRECT_MAX_BYTES]),
                    encoding=resp.encoding,
                )
    except TimeoutError as e:
        raise _DirectFailed(f"a direct GET did not finish within {_DIRECT_TIMEOUT_SECONDS:g}s") from e
    except httpx.HTTPError as e:
        raise _DirectFailed(f"a direct GET failed: {type(e).__name__}: {e}") from e


def _html_to_page(html: str) -> Page | None:
    """用 trafilatura 提取正文转成 Markdown（去掉导航、页脚等）；提取不出正文时返回 None。链接保持页面里的原样
    （相对链接不由它补全：它只按主机名补，/3/library/x.html 里的相对链接会补错）。CPU 活，在线程里跑。"""
    text = trafilatura.extract(html, output_format="markdown", include_links=True, include_tables=True)
    if not text or not text.strip():
        return None
    meta = trafilatura.extract_metadata(html)
    return Page(title=(meta.title if meta is not None and meta.title else ""), content=text)


async def _readable_page(resp: _Response) -> Page:
    text = resp.body.decode(resp.encoding, errors="replace")
    if _media_type(resp.content_type) not in _HTML_TYPES:
        return Page(title="", content=text)
    page = await anyio.to_thread.run_sync(_html_to_page, text)
    if page is None:
        raise _DirectFailed(
            "a direct GET returned HTML without extractable text (the page is probably rendered by JavaScript): "
            "open it in a browser instead"
        )
    return page


async def _direct_fetch(client: httpx.AsyncClient, url: str, service_reason: str) -> Page:
    """抓取服务拿不到或不可用（`service_reason`）时直接 GET 一次：HTML 转成 Markdown，纯文本（源码、Markdown、JSON……）
    原样返回，都注明是直连；否则把两边的原因一起交还模型（InvalidInput）。直连 404 才说明页面确实不存在，所以服务那句
    404 不能单独交还（#189）。直连失败不算暂时性失败：连不上的是目标站点，与 #134 的目标站点超时同理交还模型，
    由它决定换 URL、改用浏览器还是稍后再试。"""
    # 只记 host：URL 里可能带着对话内容
    host = httpx.URL(url).host
    try:
        resp = await _direct_get(client, url)
        page = await _readable_page(resp)
    except _DirectFailed as e:
        log.warning(
            "web request failed", extra={"provider": "direct", "op": "fetch", "host": host, "reason": str(e)}
        )
        raise InvalidInput(
            f"{service_reason}. The fetch service could not get this URL, which does not by itself mean the page is "
            f"missing; {e}"
        ) from e
    log.info("web fetch", extra={"provider": "direct", "host": host, "chars": len(page.content)})
    note = (
        f"(Fetched directly from {resp.url} with a plain GET because the fetch service could not get it; "
        f"content-type {resp.content_type}.)"
    )
    return Page(title=page.title, content=f"{note}\n\n{page.content}")


def build_web_provider(cfg: WebConfig, client: httpx.AsyncClient) -> WebProvider:
    match cfg.type:
        case "deepseek":
            # 与 LLM provider 一样，地址只来自配置，未填时显式使用官方地址
            return DeepSeekWeb(cfg.api_key.get_secret_value(), cfg.base_url or DEEPSEEK_BASE_URL, client)
        case _:
            assert_never(cfg.type)
