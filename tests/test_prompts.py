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
    assert await container.prompts.current(agent.id) == "v2"
    # 指令未变时不追加新版本
    await container.agents.update(agent.id, spec, author=agent.id)

    history = await container.prompts.history(agent.id)
    assert [(r.content, r.author) for r in history] == [("v2", "user"), ("Be helpful.", "user")]

    await container.prompts.restore(history[1].id, agent.id)
    assert await container.prompts.current(agent.id) == "Be helpful."
    assert [r.author for r in await container.prompts.history(agent.id)][0] == agent.id


async def test_prompt_api_edits_public_prompt_and_restores_versions(client, make_agent):
    agent = await make_agent("alpha")
    await login(client)
    assert (await client.get("/api/prompts")).json() == []

    for content in ("first", "second"):
        assert (await client.put("/api/prompts/public", json={"content": content})).status_code == 204
    history = (await client.get("/api/prompts")).json()
    assert [r["content"] for r in history] == ["second", "first"]

    assert (await client.post(f"/api/prompt-revisions/{history[1]['id']}/restore")).status_code == 204
    assert (await client.get("/api/prompts")).json()[0]["content"] == "first"
    agent_history = (await client.get(f"/api/prompts?owner={agent.id}")).json()
    assert [r["content"] for r in agent_history] == ["Be helpful."]
    assert (await client.get("/api/prompts?owner=nope")).status_code == 404


async def test_each_listed_agent_gets_its_own_latest_instructions(client, container, make_agent):
    for name in ("alpha", "beta"):
        agent = await make_agent(name)
        for version in ("v2", "v3"):
            spec = AgentPatch(instructions=f"{name} {version}")
            await container.agents.update(agent.id, spec, author="user")
    await login(client)
    listed = (await client.get("/api/agents")).json()
    assert {a["name"]: a["instructions"] for a in listed} == {"alpha": "alpha v3", "beta": "beta v3"}


async def test_deleting_agent_drops_its_versions_but_not_public_prompt_history(container, make_agent):
    agent = await make_agent("alpha")
    await container.prompts.set(None, "shared", agent.id)
    await container.agent_lifecycle.delete(agent.id, author="user")

    assert [r.author for r in await container.prompts.history(None)] == [agent.id]
    assert await container.prompts.history(agent.id) == []


async def test_empty_public_prompt_is_the_same_as_none(container):
    assert await container.prompts.set(None, "", "user") is None
    assert await container.prompts.history(None) == []
    assert await container.prompts.current(None) == ""
