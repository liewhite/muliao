"""agent 工作目录的只读浏览（workspace.browse 与 GET /api/agents/{id}/workspace）。"""

import os

from tests.conftest import login


async def _workspace(container, make_agent):
    agent = await make_agent()
    root = container.agents.workspace(agent.id)
    root.mkdir(parents=True, exist_ok=True)
    return agent.id, root


async def test_lists_one_level_dirs_first_and_reads_files(container, make_agent, client):
    agent, root = await _workspace(container, make_agent)
    (root / "evolve").mkdir()
    (root / "evolve" / "draft.md").write_text("# 草稿\n")
    (root / "b.txt").write_text("hi")
    (root / "a.png").write_bytes(b"\x89PNG")
    await login(client)

    top = (await client.get(f"/api/agents/{agent}/workspace")).json()
    assert [(e["name"], e["is_dir"], e["size"], e["inline"]) for e in top] == [
        ("evolve", True, None, False),
        ("a.png", False, 4, True),
        ("b.txt", False, 2, False),
    ]
    inner = (await client.get(f"/api/agents/{agent}/workspace", params={"path": "evolve"})).json()
    assert [e["name"] for e in inner] == ["draft.md"]

    md = await client.get(f"/api/agents/{agent}/workspace/file", params={"path": "evolve/draft.md"})
    assert md.status_code == 200
    assert md.text == "# 草稿\n"
    # agent 写的内容按不可信处理：非图片一律作为附件、沙箱，且不缓存（内容会变）
    assert md.headers["content-disposition"].startswith("attachment")
    assert md.headers["content-security-policy"].startswith("sandbox")
    assert md.headers["x-content-type-options"] == "nosniff"
    assert md.headers["cache-control"] == "no-cache"
    png = await client.get(f"/api/agents/{agent}/workspace/file", params={"path": "a.png"})
    assert png.headers["content-disposition"].startswith("inline")


async def test_paths_outside_the_workspace_are_rejected(container, make_agent, client, tmp_path):
    agent, root = await _workspace(container, make_agent)
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    os.symlink(outside, root / "link.txt")
    await login(client)

    for path in ("../", "/etc", "link.txt", f"../../{tmp_path.name}/secret.txt", "a\0b"):
        resp = await client.get(f"/api/agents/{agent}/workspace/file", params={"path": path})
        assert resp.status_code == 422, path
    resp = await client.get(f"/api/agents/{agent}/workspace", params={"path": ".."})
    assert resp.status_code == 422
    # 指向外面的链接打不开，也就不列出
    assert (await client.get(f"/api/agents/{agent}/workspace")).json() == []


async def test_symlinks_inside_the_workspace_are_followed(container, make_agent, client):
    agent, root = await _workspace(container, make_agent)
    (root / "real").mkdir()
    (root / "real" / "f.txt").write_text("inside")
    os.symlink(root / "real", root / "alias")
    await login(client)

    top = (await client.get(f"/api/agents/{agent}/workspace")).json()
    assert [(e["name"], e["is_dir"]) for e in top] == [("alias", True), ("real", True)]
    resp = await client.get(f"/api/agents/{agent}/workspace/file", params={"path": "alias/f.txt"})
    assert resp.text == "inside"


async def test_symlink_loops_do_not_break_the_listing(container, make_agent, client):
    agent, root = await _workspace(container, make_agent)
    (root / "f.txt").write_text("x")
    os.symlink(root / "loop", root / "loop")
    await login(client)

    top = (await client.get(f"/api/agents/{agent}/workspace")).json()
    assert [e["name"] for e in top] == ["f.txt"]
    resp = await client.get(f"/api/agents/{agent}/workspace/file", params={"path": "loop"})
    assert resp.status_code == 404


async def test_missing_paths_and_agents_are_not_found(container, make_agent, client):
    agent, root = await _workspace(container, make_agent)
    (root / "f.txt").write_text("x")
    await login(client)

    assert (await client.get(f"/api/agents/{agent}/workspace", params={"path": "nope"})).status_code == 404
    assert (await client.get("/api/agents/nobody/workspace")).status_code == 404
    # 目录与文件不能互相当作对方读
    assert (await client.get(f"/api/agents/{agent}/workspace", params={"path": "f.txt"})).status_code == 422
    assert (await client.get(f"/api/agents/{agent}/workspace/file", params={"path": ""})).status_code == 422


async def test_requires_login(container, make_agent, client):
    agent, _ = await _workspace(container, make_agent)
    assert (await client.get(f"/api/agents/{agent}/workspace")).status_code == 401
