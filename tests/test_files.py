"""对话里的文件：上传与下载的安全头、附件进入模型的形态、数据库里只存引用、agent 用 share_file 发出文件。"""

import json
import sqlite3

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.db import migrate
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
    assert [f["name"] for f in detail["open_turns"][0]["attachments"]] == ["cat.png", "data.csv"]
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


async def test_agent_shares_a_workspace_file_as_a_markdown_reference(client, container, make_agent, models):
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

    assert outputs[0].startswith("![chart.png](/api/files/") and outputs[1].startswith(
        "[report.md](/api/files/"
    )
    await login(client)
    resp = await client.get(outputs[0].removeprefix("![chart.png](").removesuffix(")"))
    assert resp.content == PNG and resp.headers["content-disposition"].startswith("inline")


def test_unfinished_uploads_are_removed_on_recover(container):
    tmp = container.settings.files_dir / "tmp"
    tmp.mkdir(parents=True)
    (tmp / "half").write_bytes(b"x")
    container.files.recover()
    assert list(tmp.iterdir()) == []


def test_migration_adds_empty_attachments_to_existing_turns(tmp_path):
    path = tmp_path / "m.db"
    migrate(path, "4a351a020890")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO tasks (kind, lane, payload, status, attempts, not_before, created_at)"
            " VALUES ('turn', 'l', '{\"prompt\": \"hi\", \"reply_to\": \"user\"}', 'failed', 1,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
    migrate(path)
    with sqlite3.connect(path) as conn:
        payload = conn.execute("SELECT payload FROM tasks").fetchone()[0]
    assert json.loads(payload) == {"prompt": "hi", "reply_to": "user", "attachments": []}


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
    outputs = await run_tools(container, models, agent.id, [("share_file", {"path": "x.tar.gz"})])
    file_id = outputs[0].split("/api/files/")[1].split("/")[0]
    assert (await container.files.get(file_id)).media_type == "application/octet-stream"
