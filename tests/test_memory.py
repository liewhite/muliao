"""记忆：全局记忆（所有 agent 共享）与 agent 的记忆，同一套规则——进每次运行的 instructions、每次修改一版、可以恢复。"""

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents.api import RevisionInfo, Subject
from muliao.agents.memory import MemoryFull
from muliao.errors import CorruptState
from tests.conftest import history, login
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


async def memory_history(container, owner: str | None) -> list[RevisionInfo]:
    return await history(container, Subject.MEMORY, owner or "", 10)


async def test_memory_api_reads_and_writes_global_and_agent_memory_and_restores_versions(
    client, container, make_agent
):
    agent = await make_agent("alpha")
    await login(client)
    assert (await client.get("/api/memory")).json() == {"content": "", "max_chars": 4000}
    assert (await client.get("/api/memory/history")).json() == []

    for content in ("first", "second"):
        resp = await client.put("/api/memory", json={"owner": None, "content": content})
        assert resp.json()["content"] == content
    history = (await client.get("/api/memory/history")).json()
    assert [(r["content"], r["author"]) for r in history] == [("second", "user"), ("first", "user")]
    assert (await client.post("/api/memory/restore", json={"rev": history[1]["rev"]})).status_code == 204
    assert await container.memory.read(None) == "first"

    for content in ("- likes tea\n", "- likes coffee\n"):
        await client.put("/api/memory", json={"owner": agent.id, "content": content})
    assert (await client.get("/api/memory", params={"owner": agent.id})).json()[
        "content"
    ] == "- likes coffee\n"
    memory = (await client.get(f"/api/memory/history?owner={agent.id}")).json()
    assert [r["content"] for r in memory][:2] == ["- likes coffee\n", "- likes tea\n"]
    await client.post("/api/memory/restore", json={"owner": agent.id, "rev": memory[1]["rev"]})
    assert await container.memory.read(agent.id) == "- likes tea\n"
    # 全局记忆不受影响
    assert await container.memory.read(None) == "first"
    assert (await client.get("/api/memory", params={"owner": "nope"})).status_code == 404
    bad = await client.post("/api/memory/restore", json={"owner": agent.id, "rev": "HEAD"})
    assert bad.status_code == 422


async def test_empty_global_memory_that_was_never_written_has_no_history(container):
    assert await container.memory.read(None) == ""
    await container.memory.write(None, "", "user")
    assert await memory_history(container, None) == []


async def test_cleared_global_memory_keeps_a_restorable_history(container):
    await container.memory.write(None, "v1", "user")
    await container.memory.write(None, "", "user")
    assert await container.memory.read(None) == ""
    assert [r.author for r in await memory_history(container, None)] == ["user", "user"]


async def test_global_memory_has_the_same_length_limit(container):
    with pytest.raises(MemoryFull, match="global memory would be"):
        await container.memory.edit(None, "", "x" * (container.memory.max_chars + 1), "user")
    assert await memory_history(container, None) == []


async def test_an_agents_memory_limit_error_tells_the_model_without_its_id(container, make_agent):
    agent = await make_agent("alpha")
    with pytest.raises(MemoryFull, match="your memory would be") as e:
        await container.memory.edit(agent.id, "", "x" * (container.memory.max_chars + 1), agent.id)
    assert agent.id not in str(e.value)


async def test_a_cleared_agent_memory_is_a_new_version(container, make_agent):
    agent = await make_agent("alpha")
    await container.memory.write(agent.id, "- fact", "user")
    await container.memory.write(agent.id, "", "user")
    assert await container.memory.read(agent.id) == ""
    assert len(await memory_history(container, agent.id)) == 3


async def test_an_agent_edits_the_global_memory_as_a_versioned_change_under_its_name(
    container, make_agent, models
):
    agent = await make_agent("alpha", model="m", roles=["admin"])
    args = {"new_text": "- all agents answer in Chinese"}
    models.models["m"] = call_then_answer("memory_edit_public", args, "Added a rule.")
    await start(container, agent.id, "hello")
    await drain(container)

    assert await container.memory.read(None) == "- all agents answer in Chinese\n"
    # 自己的记忆没变
    assert await container.memory.read(agent.id) == ""
    assert (await memory_history(container, None))[0].author == agent.id

    models.models["m"] = call_then_answer(
        "memory_edit_public", {"old_text": "Chinese", "new_text": "English"}, "Done."
    )
    await start(container, agent.id, "again")
    await drain(container)
    assert await container.memory.read(None) == "- all agents answer in English\n"


async def test_deleting_an_agent_keeps_the_global_memory_it_wrote(container, make_agent):
    agent = await make_agent("alpha")
    await container.memory.write(None, "shared", agent.id)
    await container.agent_lifecycle.delete(agent.id, author="user")
    # 删除的清理经事件完成
    await drain(container)

    assert [r.author for r in await memory_history(container, None)] == [agent.id]
    with pytest.raises(CorruptState):
        await container.memory.read(agent.id)
    # 被删 agent 的记忆的历史仍在版本表里，可以审计
    assert len(await memory_history(container, agent.id)) == 2
