"""网页搜索与抓取。`WebProvider` 是 web 工具组依赖的唯一接口，具体服务是它的实现，由配置 `[web]` 的 type 选择。

各服务自己的输入限制（query 长度、条数上限、URL 规则）以服务端校验为唯一来源，这里不重复一份：
服务端拒绝时把它的原因作为 InvalidInput 交还模型修正。
"""

import logging
from dataclasses import dataclass
from typing import Literal, Protocol, assert_never

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
    # Markdown 正文
    content: str


class WebProvider(Protocol):
    async def search(self, query: str, count: int) -> list[SearchHit]: ...

    async def fetch(self, url: str) -> Page: ...


# --- DeepSeek：https://api-docs.deepseek.com/zh-cn/guides/search ---

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# 抓取要等对方站点，给足时间；挂起时及时报错，而不是一直占着这次运行
_DEEPSEEK_TIMEOUT_SECONDS = 60.0
# 文档：408、429、5xx 等待后重试
_DEEPSEEK_TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})

_Op = Literal["search", "fetch"]
# 由模型给出的输入引起、按契约交还模型修正的状态码，按接口区分：400 字段值无效、413 请求体过大；
# fetch 另有 403 被安全策略拒绝、404 抓取不到可用内容。其余（401 key 无效、402 余额不足、search 的 403/404……）
# 是配置、账户问题或契约之外的响应，模型改不了，让这一轮失败可见
_DEEPSEEK_REJECTED_INPUT: dict[_Op, frozenset[int]] = {
    "search": frozenset({400, 413}),
    "fetch": frozenset({400, 403, 404, 413}),
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


class DeepSeekWeb:
    def __init__(self, api_key: str, base_url: str, client: httpx.AsyncClient):
        # 共用的出站 HTTP 客户端，由容器创建、在关停时关闭
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def _post(self, op: _Op, body: dict[str, object]) -> object:
        resp = await self._client.post(
            f"{self._base_url}/v1/tools/{op}",
            json=body,
            headers=self._headers,
            timeout=_DEEPSEEK_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            return resp.json()
        message = _error_message(resp)
        reason = f"deepseek {op} failed: HTTP {resp.status_code}: " + (
            message if message is not None else f"response outside the error contract: {resp.text[:500]}"
        )
        log.warning("web request failed", extra={"provider": "deepseek", "op": op, "reason": reason})
        if resp.status_code in _DEEPSEEK_TRANSIENT:
            raise TransientError(reason)
        # 只有服务按契约拒绝的输入才交还模型；状态码对上而错误体不对（多半是 base_url 指错）是配置问题
        if resp.status_code in _DEEPSEEK_REJECTED_INPUT[op] and message is not None:
            raise InvalidInput(reason)
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
        raw = await self._post("fetch", {"url": url})
        page = _DeepSeekFetchResponse.model_validate(raw)
        # 只记 host：URL 里可能带着对话内容
        host = httpx.URL(url).host
        log.info("web fetch", extra={"provider": "deepseek", "host": host, "chars": len(page.content)})
        return Page(title=page.title, content=page.content)


def build_web_provider(cfg: WebConfig, client: httpx.AsyncClient) -> WebProvider:
    match cfg.type:
        case "deepseek":
            # 与 LLM provider 一样，地址只来自配置，未填时显式使用官方地址
            return DeepSeekWeb(cfg.api_key.get_secret_value(), cfg.base_url or DEEPSEEK_BASE_URL, client)
        case _:
            assert_never(cfg.type)
