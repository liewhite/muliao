"""每个 GET 接口声明自己读哪些数据（x-reads，api.deps.reads；归属见 docs/architecture.md 2.4 第 16 行），前端按它订阅
changed。这里逐个调用全部 GET 接口、记下它实际查了哪些表，按表推出的类别必须被声明覆盖：新加的关联查询没声明就失败。
资源文件（skill、记忆、agent.toml）不在库里，查不出来，读它们的接口照实声明 agents。"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from muliao.api.app import api_schema
from muliao.schema import resource_of
from tests.conftest import login

_TABLE = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE)\s+"?(\w+)"?', re.IGNORECASE)

# 不在测试里调用的，声明为空：向 provider 实时查询（出网），事件流（本身就是变化的推送，不会结束）
_NOT_CALLED = {"/api/providers/{name}/models", "/api/events"}


@contextmanager
def _recording(engine) -> Iterator[set[str]]:
    tables: set[str] = set()

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        tables.update(_TABLE.findall(statement))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield tables
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _declared() -> dict[str, list[str]]:
    paths = api_schema()["paths"]
    return {p: ops["get"]["x-reads"] for p, ops in paths.items() if "get" in ops}


def test_every_get_endpoint_declares_what_it_reads():
    paths = api_schema()["paths"]
    missing = [p for p, ops in paths.items() if "get" in ops and "x-reads" not in ops["get"]]
    assert missing == []


async def test_get_endpoints_read_only_what_they_declare(client, container):
    await login(client)
    agent = (await client.post("/api/agents", json={"name": "alpha", "description": "d"})).json()["id"]
    session = (await client.post("/api/sessions", json={"agent_id": agent, "prompt": "hi"})).json()["id"]
    issue = (await client.post("/api/issues", json={"title": "t", "owner": agent})).json()["id"]
    skill = {"owner": None, "description": "d", "body": "steps", "version": None}
    assert (await client.put("/api/assets/skill/sk", json=skill)).status_code == 200
    mcp = {"owner": None, "transport": "http", "url": "https://example.com/mcp", "probe": False}
    assert (await client.put("/api/assets/mcp/srv", json=mcp)).status_code == 201
    assert (await client.put("/api/assets/secret/KEY", json={"value": "v"})).status_code == 204
    uploaded = (
        await client.post(
            "/api/files", params={"name": "a.txt"}, content=b"x", headers={"content-type": "text/plain"}
        )
    ).json()
    (container.settings.assets_dir / "platform" / "skills" / "sk" / "f.txt").write_text("x")

    concrete = {
        "/api/agents/{agent_id}/history": f"/api/agents/{agent}/history",
        "/api/agents/{agent_id}/workspace": f"/api/agents/{agent}/workspace",
        "/api/agents/{agent_id}/workspace/file": None,
        "/api/agents/{agent_id}/memory": f"/api/agents/{agent}/memory",
        "/api/agents/{agent_id}": f"/api/agents/{agent}",
        "/api/agents/{agent_id}/assets": f"/api/agents/{agent}/assets",
        "/api/assets/memory/{name}": "/api/assets/memory/global",
        "/api/assets/skill/{name}": "/api/assets/skill/sk",
        "/api/assets/skill/{name}/files": "/api/assets/skill/sk/files",
        "/api/assets/skill/{name}/file": "/api/assets/skill/sk/file?path=f.txt",
        "/api/assets/skill/{name}/package": "/api/assets/skill/sk/package",
        "/api/assets/mcp/{name}": "/api/assets/mcp/srv",
        "/api/assets/secret/{name}": "/api/assets/secret/KEY",
        "/api/sessions/{session_id}": f"/api/sessions/{session}",
        "/api/issues/{issue_id}": f"/api/issues/{issue}",
        "/api/files/{file_id}/{name}": f"/api/files/{uploaded['id']}/{uploaded['name']}",
        "/api/usage": "/api/usage?since=2026-01-01T00:00:00Z&until=2026-01-02T00:00:00Z",
    }
    workspace = container.settings.data_dir / "workspaces" / agent
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "w.txt").write_text("x")
    concrete["/api/agents/{agent_id}/workspace/file"] = f"/api/agents/{agent}/workspace/file?path=w.txt"

    wrong = {}
    for template, declared in _declared().items():
        if template in _NOT_CALLED:
            assert declared == [], template
            continue
        url = concrete.get(template, template)
        assert "{" not in url, f"add a concrete URL for {template}"
        with _recording(container.db.engine) as tables:
            resp = await client.get(url)
        assert resp.status_code == 200 or resp.status_code == 204, (template, resp.status_code, resp.text)
        read = {resource_of(t) for t in tables}
        if not read <= set(declared):
            wrong[template] = sorted(read - set(declared))
    assert wrong == {}, "GET endpoints read data they do not declare (x-reads)"
