"""网页搜索与抓取：DeepSeek 实现按其文档收发与归类错误；web 工具组只在配置了服务时存在。"""

import json

import anyio
import httpx
import pytest
from pydantic import ValidationError

from muliao.agent_runtime.web import DeepSeekWeb, Page, SearchHit
from muliao.agents.service import AgentSpec
from muliao.container import assemble
from muliao.errors import InvalidInput, TransientError
from muliao.platform.db import migrate
from tests.test_tools import run_tools


def _deepseek(handler) -> DeepSeekWeb:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DeepSeekWeb("sk-test", "https://ds.example/", client)


def _result(**overrides) -> dict:
    return {
        "rerank_score": 2.4,
        "title": "V4",
        "url": "https://example.com/v4",
        "snippet": "short",
        "summary": "long summary",
        "content": "",
        "site_name": None,
        "site_icon": None,
        "date": "2026-06-01T00:00:00+00:00",
        "authority": 3,
        **overrides,
    }


async def test_deepseek_search_posts_query_with_bearer_key_and_maps_results():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"results": [_result(), _result(title="B", date=None, site_name="S")]}
        )

    hits = await _deepseek(handler).search("deepseek site:github.com", 2)

    assert str(seen[0].url) == "https://ds.example/v1/tools/search"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert json.loads(seen[0].content) == {"query": "deepseek site:github.com", "count": 2}
    assert hits == [
        SearchHit("V4", "https://example.com/v4", "long summary", None, "2026-06-01T00:00:00+00:00"),
        SearchHit("B", "https://example.com/v4", "long summary", "S", None),
    ]


async def test_deepseek_fetch_returns_markdown_page():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/tools/fetch"
        assert json.loads(request.content) == {"url": "https://example.com/a"}
        return httpx.Response(200, json={"title": "", "content": "# A\n\nbody"})

    assert await _deepseek(handler).fetch("https://example.com/a") == Page("", "# A\n\nbody")


@pytest.mark.parametrize(
    ("op", "status", "error"),
    [
        ("fetch", 429, TransientError),
        ("search", 408, TransientError),
        ("search", 500, TransientError),
        ("fetch", 503, TransientError),
        ("search", 504, TransientError),
        # fetch 带错误体的 408/504 是目标站点超时，交还模型（#134）
        ("fetch", 408, InvalidInput),
        ("fetch", 504, InvalidInput),
        ("search", 400, InvalidInput),
        ("search", 413, InvalidInput),
        ("fetch", 403, InvalidInput),
        # fetch 404 先直连核实（#189）：这里直连也得到 404，所以还是交还模型
        ("fetch", 404, InvalidInput),
        # 403/404 只在 fetch 的契约里；search 收到是契约之外的响应
        ("search", 403, RuntimeError),
        ("search", 404, RuntimeError),
        ("fetch", 401, RuntimeError),
        ("search", 402, RuntimeError),
    ],
)
async def test_deepseek_errors_are_classified_by_endpoint_and_status_and_carry_the_reason(op, status, error):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"error": {"message": "nope", "type": "x", "param": None, "code": "x"}}
        return httpx.Response(status, json=body)

    web = _deepseek(handler)
    with pytest.raises(error, match=f"HTTP {status}: nope"):
        await (web.search("q", 1) if op == "search" else web.fetch("https://example.com/a"))


def _not_fetched_then(direct) -> DeepSeekWeb:
    """抓取服务对目标 URL 返回契约 404，目标站点本身由 `direct` 应答（#189）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ds.example":
            return httpx.Response(
                404, json={"error": {"message": "failed to fetch URL", "type": "not_found_error"}}
            )
        assert request.url == "https://raw.example/a.py"
        assert "muliao" in request.headers["user-agent"]
        return direct(request)

    return _deepseek(handler)


async def test_fetch_service_404_falls_back_to_a_direct_get_and_returns_plain_text_marked_as_direct():
    web = _not_fetched_then(
        lambda _r: httpx.Response(200, text="print(1)\n", headers={"content-type": "text/plain"})
    )
    page = await web.fetch("https://raw.example/a.py")
    assert page.title == ""
    assert page.content.startswith("(Fetched directly")
    assert page.content.endswith("\n\nprint(1)\n")


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (b'{"a": 1}', "application/json", '{"a": 1}'),
        (b"{}", "application/vnd.api+json", "{}"),
        ("中文".encode("gbk"), "text/plain; charset=gbk", "中文"),
    ],
)
async def test_direct_get_accepts_textual_types_and_decodes_by_charset(body, content_type, expected):
    web = _not_fetched_then(
        lambda _r: httpx.Response(200, content=body, headers={"content-type": content_type})
    )
    assert (await web.fetch("https://raw.example/a.py")).content.endswith("\n\n" + expected)


async def test_direct_get_reads_at_most_the_byte_cap(monkeypatch):
    monkeypatch.setattr("muliao.agent_runtime.web._DIRECT_MAX_BYTES", 4)
    web = _not_fetched_then(
        lambda _r: httpx.Response(200, text="abcdefgh", headers={"content-type": "text/plain"})
    )
    assert (await web.fetch("https://raw.example/a.py")).content.endswith("\n\nabcd")


async def test_direct_get_is_limited_in_total_time(monkeypatch):
    monkeypatch.setattr("muliao.agent_runtime.web._DIRECT_TIMEOUT_SECONDS", 0.05)

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b"x"
                await anyio.sleep(0.01)

    web = _not_fetched_then(
        lambda _r: httpx.Response(200, stream=Slow(), headers={"content-type": "text/plain"})
    )
    with pytest.raises(InvalidInput, match="did not finish within 0.05s"):
        await web.fetch("https://raw.example/a.py")


@pytest.mark.parametrize("status", [403, 408, 400])
async def test_other_fetch_rejections_do_not_trigger_a_direct_get(status):
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(InvalidInput):
        await _deepseek(handler).fetch("https://example.com/a")
    assert [u.host for u in seen] == ["ds.example"]


@pytest.mark.parametrize(
    ("direct", "says"),
    [
        (lambda _r: httpx.Response(404, text="Not Found"), "a direct GET returned HTTP 404"),
        (
            lambda _r: httpx.Response(
                200, text="<html>", headers={"content-type": "text/html; charset=utf-8"}
            ),
            "content-type text/html; charset=utf-8, which web_fetch does not convert",
        ),
        (
            lambda _r: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}),
            "image/png",
        ),
        (
            lambda _r: httpx.Response(200, text="<html/>", headers={"content-type": "application/xhtml+xml"}),
            "application/xhtml+xml",
        ),
        (
            lambda _r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
            "a direct GET failed: ConnectError",
        ),
    ],
)
async def test_fetch_service_404_with_failing_direct_get_returns_both_reasons_to_the_model(direct, says):
    with pytest.raises(InvalidInput) as caught:
        await _not_fetched_then(direct).fetch("https://raw.example/a.py")
    message = str(caught.value)
    assert "HTTP 404: failed to fetch URL" in message
    assert "does not by itself mean the page is missing" in message
    assert says in message


async def test_rejection_status_without_contract_error_body_is_a_config_failure_not_model_input():
    # 例如 base_url 指到了别的服务：404 页面不是 DeepSeek 的错误体，不能被当成「这个网页抓不到」交还模型
    web = _deepseek(lambda _r: httpx.Response(404, text="<html>Not Found"))
    with pytest.raises(RuntimeError, match="HTTP 404: response outside the error contract: <html>Not Found"):
        await web.fetch("https://example.com/a")


async def test_fetch_gateway_timeout_without_contract_error_body_stays_transient():
    # 网关自己的 504 页面说明服务本身不可用，不是目标站点超时
    web = _deepseek(lambda _r: httpx.Response(504, text="<html>Gateway Timeout"))
    with pytest.raises(TransientError, match="HTTP 504: response outside the error contract"):
        await web.fetch("https://example.com/a")


async def test_deepseek_transient_error_without_json_body_reports_raw_text():
    with pytest.raises(TransientError, match="HTTP 502: response outside the error contract: <html>bad"):
        await _deepseek(lambda _r: httpx.Response(502, text="<html>bad gateway")).search("q", 1)


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_deepseek_unreachable_is_its_own_transient_error_not_a_model_outage(failure):
    """#144：连不上的原始 httpx 异常冒到运行边界会被当作模型不可用，按长得多的模型重试上限重试。"""
    from muliao.agent_runtime.transient import transient_as_retryable

    def handler(_r: httpx.Request) -> httpx.Response:
        raise failure

    with pytest.raises(TransientError, match=f"deepseek search failed: {type(failure).__name__}") as caught:
        with transient_as_retryable():
            await _deepseek(handler).search("q", 1)
    assert type(caught.value) is TransientError


def test_deepseek_without_base_url_uses_the_official_address():
    from pydantic import SecretStr

    from muliao.agent_runtime.web import build_web_provider
    from muliao.config import WebConfig

    web = build_web_provider(WebConfig(type="deepseek", api_key=SecretStr("k")), httpx.AsyncClient())
    assert isinstance(web, DeepSeekWeb) and web._base_url == "https://api.deepseek.com"


async def test_deepseek_response_outside_contract_fails():
    with pytest.raises(ValidationError):
        await _deepseek(lambda _r: httpx.Response(200, json={"items": []})).search("q", 1)


class FakeWeb:
    def __init__(self) -> None:
        self.queries: list[tuple[str, int]] = []

    async def search(self, query: str, count: int) -> list[SearchHit]:
        self.queries.append((query, count))
        return [SearchHit("T", "https://t.example", "about t", None, None)]

    async def fetch(self, url: str) -> Page:
        if url == "https://gone.example":
            raise InvalidInput("deepseek fetch failed: HTTP 404: no content")
        return Page("Title", "x" * 50)


@pytest.fixture
async def web_container(settings, models):
    settings = settings.model_copy(
        update={"runtime": settings.runtime.model_copy(update={"tool_output_max_chars": 30})}
    )
    web = FakeWeb()
    c = assemble(settings, resolve_model=models, model_checker=lambda _m, _t: None, web=web)
    migrate(settings.db_path)
    yield c, web
    await c.db.engine.dispose()


async def test_web_agent_searches_and_fetches_with_truncation_and_rejections_go_back_to_model(
    web_container, models
):
    container, web = web_container
    agent = await container.agents.create(
        AgentSpec(name="reader", description="d", model="tools", allow=["web:*"], roles=[]),
        author="user",
    )

    outputs = await run_tools(
        container,
        models,
        agent.id,
        [
            ("web_search", {"query": "t"}),
            ("web_fetch", {"url": "https://t.example"}),
            ("web_fetch", {"url": "https://gone.example"}),
        ],
    )

    assert web.queries == [("t", 10)]
    assert "about t" in outputs[0]
    assert outputs[1] == "# Title\n\n" + "x" * 21 + "…[truncated 29 chars]"
    assert "HTTP 404: no content" in outputs[2]


async def test_without_web_config_web_permissions_grant_no_tools_and_does_not_block_startup(web_container):
    # 同一份数据先在配置了 web 时选上它，再换成没配置的进程：角色只是规则，没注册的工具就没有
    with_web, _ = web_container
    reader = await with_web.agents.create(
        AgentSpec(name="reader", description="d", allow=["web:*"], roles=[]), author="user"
    )
    without_web = assemble(with_web.settings, model_checker=lambda _m, _t: None)
    await without_web.agents.verify_stored()
    toolbox = without_web.consumer._toolbox  # noqa: SLF001
    assert "web_search" not in toolbox.tool_names()
    # 界面与 get_agent 也不列出这次配置没有的工具（与运行时取同一份注册的工具）
    assert "web_search" not in without_web.agents.resolve(await without_web.agents.rules(reader)).tools
    assert "web_search" in with_web.agents.resolve(await with_web.agents.rules(reader)).tools
    await without_web.db.engine.dispose()


def test_ui_version_changes_with_the_build_and_is_absent_without_one(tmp_path):
    from muliao.api.app import ui_version

    assert ui_version(tmp_path) is None
    (tmp_path / "index.html").write_text('<script src="/assets/index-AAA.js"></script>')
    first = ui_version(tmp_path)
    (tmp_path / "index.html").write_text('<script src="/assets/index-BBB.js"></script>')
    assert first is not None and ui_version(tmp_path) not in (None, first)
