"""prompt 版本化：公共 prompt 合并进所有 agent，agent 指令每次修改追加一版，可以恢复旧版。"""

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents import AgentPatch
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, start


async def test_public_prompt_precedes_agent_instructions_in_every_run(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    await container.prompts.set(None, "Always answer in Chinese.", "user")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "hi")
    await drain(container)

    assert seen[0].index("Always answer in Chinese.") < seen[0].index("Be helpful.")


async def test_agent_instruction_changes_are_versioned_and_restorable(container, make_agent):
    agent = await make_agent("alpha")
    spec = AgentPatch(instructions="v2")
    await container.agents.update(agent.id, spec, author="user")
    assert container.prompts.current(agent.id) == "v2"
    # 指令未变时不追加新版本
    await container.agents.update(agent.id, spec, author=agent.id)

    history = await container.prompts.history(agent.id, 10)
    contents = [await container.prompts.read_at(agent.id, r.rev) for r in history]
    assert list(zip(contents, [r.author for r in history], strict=True)) == [
        ("v2", "user"),
        ("Be helpful.", "user"),
    ]

    await container.prompts.restore(agent.id, history[1].rev, agent.id)
    assert container.prompts.current(agent.id) == "Be helpful."
    assert (await container.prompts.history(agent.id, 10))[0].author == agent.id


async def test_resource_api_edits_public_prompt_and_restores_prompt_and_memory(client, container, make_agent):
    agent = await make_agent("alpha")
    await login(client)
    assert (await client.get("/api/resources/prompt/history")).json() == []

    for content in ("first", "second"):
        assert (await client.put("/api/prompts/public", json={"content": content})).status_code == 204
    history = (await client.get("/api/resources/prompt/history")).json()
    assert [r["content"] for r in history] == ["second", "first"]

    resp = await client.post("/api/resources/prompt/restore", json={"rev": history[1]["rev"]})
    assert resp.status_code == 204
    assert (await client.get("/api/resources/prompt/history")).json()[0]["content"] == "first"
    agent_history = (await client.get(f"/api/resources/prompt/history?owner={agent.id}")).json()
    assert [r["content"] for r in agent_history] == ["Be helpful."]
    assert (await client.get("/api/resources/prompt/history?owner=nope")).status_code == 404

    # 记忆同样有历史，可以恢复
    for content in ("- likes tea\n", "- likes coffee\n"):
        await client.put(f"/api/agents/{agent.id}/memory", json={"content": content})
    memory = (await client.get(f"/api/resources/memory/history?owner={agent.id}")).json()
    assert [r["content"] for r in memory][:2] == ["- likes coffee\n", "- likes tea\n"]
    await client.post("/api/resources/memory/restore", json={"owner": agent.id, "rev": memory[1]["rev"]})
    assert container.memory.read(agent.id) == "- likes tea\n"
    assert (await client.get("/api/resources/memory/history")).status_code == 422
    bad = await client.post("/api/resources/memory/restore", json={"owner": agent.id, "rev": "HEAD"})
    assert bad.status_code == 422


async def test_each_listed_agent_gets_its_own_latest_instructions(client, container, make_agent):
    for name in ("alpha", "beta"):
        agent = await make_agent(name)
        for version in ("v2", "v3"):
            spec = AgentPatch(instructions=f"{name} {version}")
            await container.agents.update(agent.id, spec, author="user")
    await login(client)
    listed = (await client.get("/api/agents")).json()
    assert {a["name"]: a["instructions"] for a in listed} == {"alpha": "alpha v3", "beta": "beta v3"}


async def test_deleting_agent_drops_its_files_but_not_public_prompt_history(container, make_agent):
    agent = await make_agent("alpha")
    await container.prompts.set(None, "shared", agent.id)
    await container.agent_lifecycle.delete(agent.id, author="user")

    assert [r.author for r in await container.prompts.history(None, 10)] == [agent.id]
    assert not container.prompts.path(agent.id).exists()
    # 被删 agent 的历史仍在仓库里，可以审计
    assert len(await container.resources.history(container.prompts.path(agent.id), 10)) == 2


async def test_empty_public_prompt_is_the_same_as_none(container):
    assert await container.prompts.set(None, "", "user") is False
    assert await container.prompts.history(None, 10) == []
    assert container.prompts.current(None) == ""


async def test_reflection_refines_own_instructions_as_a_versioned_change(container, make_agent, models):
    from pydantic_ai.messages import ToolCallPart

    from tests.test_flow import last_user_prompt, tool_returns

    agent = await make_agent("alpha", model="m", reflect_every=1)
    seen_tools: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "scheduled self-review" not in last_user_prompt(messages):
            return ModelResponse(parts=[TextPart("done")])
        if tool_returns(messages):
            return ModelResponse(parts=[TextPart("Added a rule.")])
        seen_tools.extend(t.name for t in info.function_tools)
        args = {"old_text": "", "new_text": "Always reply in Chinese."}
        return ModelResponse(parts=[ToolCallPart("instructions_replace", args)])

    models.models["m"] = scripted(fn)
    await start(container, agent.id, "hello")
    await drain(container)

    assert container.prompts.current(agent.id) == "Be helpful.\n\nAlways reply in Chinese.\n"
    assert (await container.prompts.history(agent.id, 10))[0].author == agent.id
    # 反思只开放自我改进；该 agent 有 public_skills（默认全部开放）所以也能写公共 skill
    assert "instructions_replace" in seen_tools and "run_shell" not in seen_tools


async def test_agent_without_resource_files_can_still_be_deleted(container, make_agent):
    agent = await make_agent("alpha")
    await container.resources.apply({container.resources.owner_dir(agent.id): None}, "lost", "user")

    await container.agent_lifecycle.delete(agent.id, author="user")

    assert await container.agents.list() == []


async def test_mcp_api_rejects_path_traversal_owners(client, make_agent):
    await login(client)
    for owner in ("..", "../public"):
        assert (await client.post("/api/mcp/tools", params={"name": "x", "owner": owner})).status_code == 422
