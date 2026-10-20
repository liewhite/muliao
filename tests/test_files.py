"""对话里的文件：上传与下载的安全头、附件进入模型的形态、数据库里只存引用、agent 用 share_file 发出文件。"""

import json

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.models import Message
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain
from tests.test_tools import run_tools

PNG = b"\x89PNG\r\n\x1a\nfake"


async def _upload(client, name: str, data: bytes, media_type: str) -> dict:
    resp = await client.post(
        "/api/files", params={"name": name}, content=data, headers={"content-type": media_type}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_uploaded_image_is_served_inline_and_other_types_only_as_sandboxed_downloads(client):
    await login(client)
    image = await _upload(client, "图 1.png", PNG, "image/png")
    page = await _upload(client, "x.html", b"<script>alert(1)</script>", "text/html")

    resp = await client.get(image["url"])
    assert resp.content == PNG and resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"].startswith("sandbox")

    resp = await client.get(page["url"])
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["content-security-policy"].startswith("sandbox")
    # 一个文件只有一个地址：名称不对就是不存在
    assert (await client.get(f"/api/files/{page['id']}/other.html")).status_code == 404


async def test_upload_rejects_bad_names_missing_type_and_oversize(client, container):
    await login(client)
    for name in ("../x", "a/b", "", "a\nb"):
        resp = await client.post(
            "/api/files", params={"name": name}, content=b"x", headers={"content-type": "text/plain"}
        )
        assert resp.status_code == 422, name
    assert (await client.post("/api/files", params={"name": "a"}, content=b"x")).status_code == 422
    container.files.max_bytes = 3
    resp = await client.post(
        "/api/files", params={"name": "a"}, content=b"four", headers={"content-type": "text/plain"}
    )
    assert resp.status_code == 413
    assert list((container.settings.files_dir / "tmp").iterdir()) == []  # noqa: ASYNC240


async def test_files_require_login(client):
    assert (await client.post("/api/files", params={"name": "a"}, content=b"x")).status_code == 401


async def test_same_content_is_stored_once_under_two_ids(container):
    async def chunks():
        yield b"same"

    a = await container.files.put("a.txt", "text/plain", chunks())
    b = await container.files.put("b.txt", "text/plain", chunks())
    assert a.id != b.id and a.sha256 == b.sha256
    assert container.files.blob_path(a) == container.files.blob_path(b)


async def test_image_reaches_the_model_as_content_but_is_stored_as_a_reference(
    client, container, make_agent, models
):
    await login(client)
    agent = await make_agent("seer", model="vision")
    image = await _upload(client, "cat.png", PNG, "image/png")
    doc = await _upload(client, "data.csv", b"a,b\n1,2\n", "text/csv")
    seen: list[list] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        first = messages[0]
        assert isinstance(first, ModelRequest) and isinstance(first.parts[0], UserPromptPart)
        seen.append(list(first.parts[0].content))
        return ModelResponse(parts=[TextPart("a cat")])

    models.models["vision"] = scripted(fn)
    resp = await client.post(
        "/api/sessions",
        json={"agent_id": agent.id, "prompt": "what is this?", "attachments": [image["id"], doc["id"]]},
    )
    assert resp.status_code == 201
    session_id = resp.json()["id"]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert [f["name"] for f in detail["mail"][0]["attachments"]] == ["cat.png", "data.csv"]
    await drain(container)

    content = seen[0]
    assert content[0] == "what is this?"
    assert any(isinstance(c, BinaryContent) and c.data == PNG for c in content)
    notes = [c for c in content if isinstance(c, str) and c.startswith("[Attached file")]
    assert len(notes) == 2 and f"uploads/{doc['id']}/data.csv" in notes[1]
    # csv 不作为二进制交给模型，只在工作目录里
    assert sum(isinstance(c, BinaryContent) for c in content) == 1
    workspace = container.agents.workspace(agent.id)
    assert (workspace / "uploads" / doc["id"] / "data.csv").read_bytes() == b"a,b\n1,2\n"

    async with container.db.transaction() as s:
        payloads = (await s.scalars(select(Message.payload).where(Message.session_id == session_id))).all()
    stored = json.loads(payloads[0])["parts"][0]["content"]
    assert stored[1]["kind"] == "image-url" and stored[1]["url"] == image["url"]
    assert all("data" not in c for c in stored if isinstance(c, dict))

    items = (await client.get(f"/api/sessions/{session_id}")).json()["items"]
    assert items[0]["text"] == "what is this?"
    assert [(a["name"], a["url"]) for a in items[0]["attachments"]] == [
        ("cat.png", image["url"]),
        ("data.csv", doc["url"]),
    ]


async def test_message_may_be_only_attachments_but_they_must_exist(client, make_agent):
    await login(client)
    agent = await make_agent()
    image = await _upload(client, "only.png", PNG, "image/png")
    resp = await client.post(
        "/api/sessions", json={"agent_id": agent.id, "prompt": "", "attachments": [image["id"]]}
    )
    assert resp.status_code == 201 and resp.json()["title"] == "only.png"
    resp = await client.post(
        f"/api/sessions/{resp.json()['id']}/messages", json={"prompt": "", "attachments": ["nope"]}
    )
    assert resp.status_code == 404


async def _shared_attachments(client) -> list[dict]:
    """用户正在看的（顶层）会话里，工具结果挂上的文件：界面显示用的就是这些。"""
    await login(client)
    sessions = [s for s in (await client.get("/api/sessions")).json() if s["parent_session_id"] is None]
    assert len(sessions) == 1
    items = (await client.get(f"/api/sessions/{sessions[0]['id']}")).json()["items"]
    return [a for i in items if i["type"] == "tool_result" for a in i["attachments"]]


async def test_shared_files_are_shown_to_the_user_without_the_model_repeating_them(
    client, container, make_agent, models
):
    agent = await make_agent("painter", model="tools")
    workspace = container.agents.workspace(agent.id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "chart.png").write_bytes(PNG)
    (workspace / "report.md").write_text("# r")

    outputs = await run_tools(
        container,
        models,
        agent.id,
        [("share_file", {"path": "chart.png"}), ("share_file", {"path": "report.md"})],
    )

    # 模型只看到一句说明，不需要也不该再嵌入引用
    assert outputs == [
        "Shared 'chart.png' with the user; it is shown with your reply.",
        "Shared 'report.md' with the user; it is shown with your reply.",
    ]
    shared = await _shared_attachments(client)
    assert [(a["name"], a["inline"]) for a in shared] == [("chart.png", True), ("report.md", False)]
    resp = await client.get(shared[0]["url"])
    assert resp.content == PNG and resp.headers["content-disposition"].startswith("inline")


async def test_files_shared_by_a_temp_agent_reach_the_user_through_the_caller(
    client, container, make_agent, models
):
    me = await make_agent("manager", model="tools")

    def helper(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        step = sum(isinstance(m, ModelResponse) for m in messages)
        if step == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart("write_file", {"path": "plot.svg", "content": "<svg/>"}, tool_call_id="w")
                ]
            )
        if step == 1:
            return ModelResponse(parts=[ToolCallPart("share_file", {"path": "plot.svg"}, tool_call_id="s")])
        return ModelResponse(parts=[TextPart("plotted")])

    models.models["helper"] = scripted(helper)
    outputs = await run_tools(
        container, models, me.id, [("run_temp_agent", {"task": "plot it", "model": "helper"})]
    )

    assert outputs[0].startswith("plotted") and "'plot.svg'" in outputs[0]
    shared = await _shared_attachments(client)
    # SVG 能带脚本，不内联显示，只能下载
    assert [(a["name"], a["media_type"], a["inline"]) for a in shared] == [
        ("plot.svg", "image/svg+xml", False)
    ]


def test_unfinished_uploads_are_removed_on_recover(container):
    tmp = container.settings.files_dir / "tmp"
    tmp.mkdir(parents=True)
    (tmp / "half").write_bytes(b"x")
    container.files.recover()
    assert list(tmp.iterdir()) == []


async def test_image_from_an_earlier_turn_is_still_expanded_after_loading_history(
    client, container, make_agent, models
):
    await login(client)
    agent = await make_agent("seer", model="vision")
    image = await _upload(client, "cat.png", PNG, "image/png")
    binaries: list[int] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        binaries.append(
            sum(
                isinstance(c, BinaryContent)
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
                if isinstance(p, UserPromptPart) and not isinstance(p.content, str)
                for c in p.content
            )
        )
        return ModelResponse(parts=[TextPart("ok")])

    models.models["vision"] = scripted(fn)
    resp = await client.post(
        "/api/sessions", json={"agent_id": agent.id, "prompt": "look", "attachments": [image["id"]]}
    )
    session_id = resp.json()["id"]
    await drain(container)
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "and now?"})
    await drain(container)

    # 第二轮的历史从数据库读回（存储形态），发给模型前仍展开成内容
    assert binaries == [1, 1]
    async with container.db.transaction() as s:
        payloads = (await s.scalars(select(Message.payload).where(Message.session_id == session_id))).all()
    assert all(PNG.hex() not in p and "iVBOR" not in p for p in payloads)


async def test_names_are_limited_in_utf8_bytes_and_whitespace_prompt_titles_by_file(client, make_agent):
    await login(client)
    resp = await client.post(
        "/api/files", params={"name": "文" * 86}, content=b"x", headers={"content-type": "text/plain"}
    )
    assert resp.status_code == 422 and "bytes" in resp.text
    assert (
        await client.post(
            "/api/files",
            params={"name": "文" * 85},
            content=b"x",
            headers={"content-type": "text/plain; charset=utf-8"},
        )
    ).json()["media_type"] == "text/plain"

    agent = await make_agent()
    image = await _upload(client, "only.png", PNG, "image/png")
    resp = await client.post(
        "/api/sessions", json={"agent_id": agent.id, "prompt": "  ", "attachments": [image["id"]]}
    )
    assert resp.json()["title"] == "only.png"


async def test_storing_existing_content_never_rewrites_the_original(container, monkeypatch):
    async def chunks():
        yield b"same"

    first = await container.files.put("a.txt", "text/plain", chunks())
    replaced: list[object] = []
    monkeypatch.setattr("muliao.files.os.replace", lambda *a: replaced.append(a))
    second = await container.files.put("b.txt", "text/plain", chunks())
    assert replaced == [] and container.files.read(second) == container.files.read(first) == b"same"
    assert list((container.settings.files_dir / "tmp").iterdir()) == []  # noqa: ASYNC240


async def test_shared_compressed_file_is_not_labelled_as_its_inner_type(container, make_agent, models):
    agent = await make_agent("packer", model="tools")
    workspace = container.agents.workspace(agent.id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "x.tar.gz").write_bytes(b"\x1f\x8b")
    await run_tools(container, models, agent.id, [("share_file", {"path": "x.tar.gz"})])
    async with container.db.transaction() as s:
        payloads = (await s.scalars(select(Message.payload))).all()
    metadata = [p for p in payloads if "x.tar.gz" in p and '"muliao_attachments"' in p]
    assert metadata and '"media_type":"application/octet-stream"' in metadata[0]


async def test_files_shared_by_an_asked_agent_reach_the_user_in_the_asking_session(
    client, container, make_agent, models
):
    me = await make_agent("manager", model="tools")
    painter = await make_agent("painter", model="painter")
    workspace = container.agents.workspace(painter.id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "chart.png").write_bytes(PNG)

    def paint(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if sum(isinstance(m, ModelResponse) for m in messages) == 0:
            return ModelResponse(parts=[ToolCallPart("share_file", {"path": "chart.png"}, tool_call_id="s")])
        return ModelResponse(parts=[TextPart("here it is")])

    models.models["painter"] = scripted(paint)
    outputs = await run_tools(
        container,
        models,
        me.id,
        [("ask_agent", {"agent": "painter", "message": "draw", "thread": "chart"})],
    )

    assert outputs[0].startswith("here it is") and "'chart.png'" in outputs[0]
    assert [a["name"] for a in await _shared_attachments(client)] == ["chart.png"]


def test_attachments_must_reference_the_file_store():
    import pytest
    from pydantic_ai.messages import ToolReturnPart

    from muliao.conventions import TOOL_ATTACHMENTS, tool_attachments

    bad = {"url": "https://evil.example/x.png", "name": "x.png", "media_type": "image/png", "inline": True}
    part = ToolReturnPart("t", "ok", tool_call_id="c", metadata={TOOL_ATTACHMENTS: [bad]})
    with pytest.raises(RuntimeError, match="not a file reference"):
        tool_attachments(part)
    # 其他工具自己的 metadata 不受这条约定影响
    assert (
        tool_attachments(ToolReturnPart("t", "ok", tool_call_id="c", metadata={"attachments": "anything"}))
        == []
    )


async def test_live_tool_result_events_carry_shared_files(container, make_agent, models):
    import asyncio

    agent = await make_agent("painter", model="tools")
    workspace = container.agents.workspace(agent.id)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "chart.png").write_bytes(PNG)
    events = []

    async def collect():
        async with container.bus.subscribe() as sub:
            async for e in sub:
                events.append(e)

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await run_tools(container, models, agent.id, [("share_file", {"path": "chart.png"})])
    collector.cancel()

    tool_items = [e.item for e in events if e.type == "run" and e.event == "item"]
    assert [(i.type, [a["name"] for a in i.attachments]) for i in tool_items] == [
        ("tool_call", []),
        ("tool_result", ["chart.png"]),
    ]
