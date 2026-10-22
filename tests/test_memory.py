"""记忆：全局记忆（所有 agent 共享）与 agent 的记忆，同一套规则——进每次运行的 instructions、每次修改一版、可以恢复。"""

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents.api import Revision
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import call_then_answer, drain, start


async def test_global_memory_precedes_the_agents_own_memory_in_every_run(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    await container.memory.write(None, "Always answer in Chinese.", "user")
    await container.memory.write(alpha, "- likes tea", "user")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "hi")
    await drain(container)

    assert seen[0].index("Always answer in Chinese.") < seen[0].index("## Runtime context")
    assert seen[0].index("## Runtime context") < seen[0].index("- likes tea")


async def test_a_corrupt_public_skill_is_listed_as_corrupt_in_the_index_and_the_run_goes_on(
    container, make_agent, models
):
    alpha = (await make_agent("alpha", model="m")).id
    await container.skills.write(None, "good", "a good skill", "x", "user")
    broken = container.resources.owner_dir(None) / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [oops\n---\nbody")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "hi")
    await drain(container)

    index = seen[0][seen[0].index("## Skill index") :]
    assert "- `good` (public): a good skill" in index
    assert "- public skill 'broken' is corrupt and cannot be used until it is rewritten or deleted:" in index


async def memory_history(container, owner: str | None) -> list[Revision]:
    return await container.resources.history(container.memory.path(owner), 10)


async def test_memory_api_reads_and_writes_global_and_agent_memory_and_restores_versions(
    client, container, make_agent
):
    agent = await make_agent("alpha")
    await login(client)
    assert (await client.get("/api/memory")).json() == {"content": "", "max_chars": 4000}
    assert (await client.get("/api/resources/memory/history")).json() == []

    for content in ("first", "second"):
        resp = await client.put("/api/memory", json={"owner": None, "content": content})
        assert resp.json()["content"] == content
    history = (await client.get("/api/resources/memory/history")).json()
    assert [(r["content"], r["author"]) for r in history] == [("second", "user"), ("first", "user")]
    assert (
        await client.post("/api/resources/memory/restore", json={"rev": history[1]["rev"]})
    ).status_code == 204
    assert container.memory.read(None) == "first"

    for content in ("- likes tea\n", "- likes coffee\n"):
        await client.put("/api/memory", json={"owner": agent.id, "content": content})
    assert (await client.get("/api/memory", params={"owner": agent.id})).json()[
        "content"
    ] == "- likes coffee\n"
    memory = (await client.get(f"/api/resources/memory/history?owner={agent.id}")).json()
    assert [r["content"] for r in memory][:2] == ["- likes coffee\n", "- likes tea\n"]
    await client.post("/api/resources/memory/restore", json={"owner": agent.id, "rev": memory[1]["rev"]})
    assert container.memory.read(agent.id) == "- likes tea\n"
    # 全局记忆不受影响
    assert container.memory.read(None) == "first"
    assert (await client.get("/api/memory", params={"owner": "nope"})).status_code == 404
    bad = await client.post("/api/resources/memory/restore", json={"owner": agent.id, "rev": "HEAD"})
    assert bad.status_code == 422


async def test_empty_global_memory_that_was_never_written_has_no_history(container):
    assert container.memory.read(None) == ""
    await container.memory.write(None, "", "user")
    assert await memory_history(container, None) == []


async def test_cleared_global_memory_keeps_a_restorable_history(container):
    await container.memory.write(None, "v1", "user")
    await container.memory.write(None, "", "user")
    assert container.memory.read(None) == ""
    assert [r.author for r in await memory_history(container, None)] == ["user", "user"]


async def test_global_memory_has_the_same_length_limit(container):
    from muliao.agents.memory import MemoryFull

    try:
        await container.memory.append(None, "x" * (container.memory.max_chars + 1), "user")
    except MemoryFull as e:
        assert "global memory would be" in str(e)
    else:
        raise AssertionError("expected MemoryFull")
    assert await memory_history(container, None) == []


async def test_an_agent_edits_the_global_memory_as_a_versioned_change_under_its_name(
    container, make_agent, models
):
    agent = await make_agent("alpha", model="m")
    args = {"entry": "- all agents answer in Chinese", "scope": "public"}
    models.models["m"] = call_then_answer("memory_append", args, "Added a rule.")
    await start(container, agent.id, "hello")
    await drain(container)

    assert container.memory.read(None) == "- all agents answer in Chinese\n"
    # 自己的记忆没变
    assert container.memory.read(agent.id) == ""
    assert (await memory_history(container, None))[0].author == agent.id

    models.models["m"] = call_then_answer(
        "memory_replace", {"old_text": "Chinese", "new_text": "English", "scope": "public"}, "Done."
    )
    await start(container, agent.id, "again")
    await drain(container)
    assert container.memory.read(None) == "- all agents answer in English\n"


async def test_deleting_an_agent_keeps_the_global_memory_it_wrote(container, make_agent):
    agent = await make_agent("alpha")
    await container.memory.write(None, "shared", agent.id)
    await container.agent_lifecycle.delete(agent.id, author="user")
    # 删除的清理经事件完成
    await drain(container)

    assert [r.author for r in await memory_history(container, None)] == [agent.id]
    assert not container.memory.path(agent.id).exists()
    # 被删 agent 的历史仍在仓库里，可以审计
    assert len(await memory_history(container, agent.id)) == 2


async def test_agent_without_resource_files_can_still_be_deleted(container, make_agent):
    agent = await make_agent("alpha")
    await container.resources.apply({container.resources.owner_dir(agent.id): None}, "lost", "user")

    await container.agent_lifecycle.delete(agent.id, author="user")
    # 删除的清理经事件完成
    await drain(container)

    assert await container.agents.list() == []


async def test_mcp_api_rejects_path_traversal_owners(client, make_agent):
    await login(client)
    for owner in ("..", "../public"):
        assert (await client.post("/api/mcp/tools", params={"name": "x", "owner": owner})).status_code == 422
