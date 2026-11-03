"""agent 自定义页面（workspace.sites 与 GET /api/pages、/sites/...，#355）。"""

import os

import pytest

from muliao.errors import NotFound
from tests.conftest import login


async def _site(container, make_agent):
    agent = await make_agent()
    root = container.agents.workspace(agent.id)
    (root / "site").mkdir(parents=True)
    return agent.id, root


def _page(root, name, html="<html><head><title>T</title></head></html>"):
    d = root / "site" / name
    d.mkdir()
    (d / "index.html").write_text(html)
    return d


async def test_lists_pages_with_title_and_signed_url(container, make_agent, client):
    agent, root = await _site(container, make_agent)
    _page(root, "mon", "<html><head><title>\n  监控 &amp; 告警 </title></head></html>")
    _page(root, "bare", "<p>no title</p>")
    (root / "site" / "not-a-page").mkdir()
    await login(client)

    pages = (await client.get("/api/pages")).json()
    assert [(p["agent_id"], p["name"], p["title"]) for p in pages] == [
        (agent, "bare", "bare"),
        (agent, "mon", "监控 & 告警"),
    ]
    assert pages[1]["url"].startswith(f"/sites/{agent}/mon/") and pages[1]["url"].endswith("/")


async def test_listing_needs_login(client):
    assert (await client.get("/api/pages")).status_code == 401


async def test_page_files_are_served_sandboxed_by_signed_url_without_cookie(container, make_agent, client):
    agent, root = await _site(container, make_agent)
    d = _page(root, "mon")
    (d / "data.json").write_text('{"x": 1}')
    await login(client)
    url = (await client.get("/api/pages")).json()[0]["url"]
    client.cookies.clear()

    index = await client.get(url)
    assert index.status_code == 200
    assert "<title>T</title>" in index.text
    assert index.headers["content-type"].startswith("text/html")
    # 脚本能跑，但不与界面同源：拿不到登录 cookie、调不了 /api
    csp = index.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
    assert index.headers["access-control-allow-origin"] == "*"
    assert index.headers["referrer-policy"] == "no-referrer"
    assert index.headers["cache-control"] == "no-cache"
    data = await client.get(url + "data.json")
    assert data.json() == {"x": 1}

    # 不带结尾斜杠补上，页面里的相对路径才解析到页面目录
    redirect = await client.get(url.rstrip("/"))
    assert redirect.status_code == 307 and redirect.headers["location"] == url


async def test_wrong_signature_or_other_page_is_rejected(container, make_agent, client):
    agent, root = await _site(container, make_agent)
    _page(root, "a")
    _page(root, "b")
    await login(client)
    url_a = next(p["url"] for p in (await client.get("/api/pages")).json() if p["name"] == "a")
    sig = url_a.rstrip("/").rsplit("/", 1)[1]

    assert (await client.get(f"/sites/{agent}/b/{sig}/")).status_code == 404
    assert (await client.get(f"/sites/{agent}/a/{'0' * len(sig)}/")).status_code == 404
    assert (await client.get(f"/sites/{agent}/a/签名/")).status_code == 404


async def test_paths_outside_the_page_are_rejected(container, make_agent, client, tmp_path):
    agent, root = await _site(container, make_agent)
    d = _page(root, "mon")
    (root / "secret.txt").write_text("x")
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    os.symlink(outside, d / "link.txt")
    await login(client)
    url = (await client.get("/api/pages")).json()[0]["url"]

    for path in ("..%2Fsecret.txt", "link.txt", "%2Fetc%2Fpasswd"):
        resp = await client.get(url + path)
        assert resp.status_code in (404, 422), path
    assert (await client.get(url + "missing.js")).status_code == 404


async def test_pages_linking_outside_the_workspace_are_not_listed(container, make_agent, client, tmp_path):
    agent, root = await _site(container, make_agent)
    outside = tmp_path / "ext"
    outside.mkdir()
    (outside / "index.html").write_text("x")
    os.symlink(outside, root / "site" / "ext")
    await login(client)
    assert (await client.get("/api/pages")).json() == []


async def test_a_directory_with_trailing_slash_serves_its_index(container, make_agent, client):
    agent, root = await _site(container, make_agent)
    d = _page(root, "mon")
    (d / "sub").mkdir()
    (d / "sub" / "index.html").write_text("inner")
    await login(client)
    url = (await client.get("/api/pages")).json()[0]["url"]

    assert (await client.get(url + "sub/")).text == "inner"
    # 不带 / 的目录不补 index：页面里的相对路径会解析到上一级
    assert (await client.get(url + "sub")).status_code == 404


async def test_index_files_linking_outside_are_neither_listed_nor_served(
    container, make_agent, client, tmp_path
):
    agent, root = await _site(container, make_agent)
    outside = tmp_path / "out.html"
    outside.write_text("<title>secret</title>")
    d = _page(root, "mon")
    (d / "sub").mkdir()
    os.symlink(outside, d / "sub" / "index.html")
    ext = root / "site" / "ext"
    ext.mkdir()
    os.symlink(outside, ext / "index.html")
    # 页面目录必须在 site 内：指回工作目录根的不算页面
    (root / "index.html").write_text("root")
    os.symlink(root, root / "site" / "up")
    await login(client)

    pages = (await client.get("/api/pages")).json()
    assert [p["name"] for p in pages] == ["mon"]
    resp = await client.get(pages[0]["url"] + "sub/")
    assert resp.status_code == 422
    with pytest.raises(NotFound):
        container.container.sites.file(agent, "ext", "")
    with pytest.raises(NotFound):
        container.container.sites.file(agent, "up", "")
