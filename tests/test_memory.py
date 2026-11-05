"""记忆是 AI 资产（#365）：全局记忆、共享记忆与私有记忆同一套规则——能用（use）的每一块进每次运行的 instructions，能改
（write）的才能改，每次修改一版、可以恢复。"""

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents.api import RevisionInfo, RoleSpec, Subject
from muliao.agents.memory import MemoryFull
from muliao.agents.revisions import record_in
from muliao.agents.service import AgentPatch, AgentSpec
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.platform.text_edit import TextChange
from tests.conftest import history, login, owned
from tests.scripted import scripted
from tests.test_flow import call_then_answer, drain, start


async def memory_history(container, name: str) -> list[RevisionInfo]:
    return await history(container, Subject.MEMORY, name, 10)


async def _content(container, name: str) -> str:
    return (await container.memory.read(name)).content


async def test_every_memory_block_an_agent_uses_is_in_its_instructions_by_name(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    await make_agent("beta")
    await container.memory.put("global", "Always answer in Chinese.", "", actor="user")
    await container.memory.put("alpha", "- likes tea", "", actor="user")
    await container.memory.put("beta", "- secret of beta", "", actor="user")
    await container.memory.create("trading", "- fees matter", actor="user")
    await container.memory.create("ops", "- not for alpha", actor="user")
    await container.distribution.create(RoleSpec("trader", "t", ["use:memory:trading"], []), actor="user")
    await container.agents.update(alpha, AgentPatch(roles=["trader"]), actor="user")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "hi")
    await drain(container)

    text = seen[0]
    # 按名字排序，每块标出能不能改
    assert text.index(
        '<memory name="alpha" access="editable, your private memory">\n- likes tea'
    ) < text.index('<memory name="global" access="read-only, shared">\nAlways answer in Chinese.')
    assert text.index('name="global"') < text.index('<memory name="trading" access="read-only, shared">')
    assert "secret of beta" not in text and "not for alpha" not in text
    assert text.index("## Runtime context") < text.index("## Memory")


async def test_memory_api_reads_writes_creates_deletes_and_restores_versions(client, container, make_agent):
    agent = await make_agent("alpha")
    await login(client)
    got = (await client.get("/api/items/memory/global")).json()
    assert (got["content"], got["owner"], got["max_chars"]) == ("", None, 4000)

    for content in ("first", "second"):
        resp = await client.put("/api/items/memory/global", json={"content": content})
        assert resp.json()["content"] == content
    revs = (await client.get("/api/items/memory/global/history")).json()
    assert [(r["content"], r["actor"]) for r in revs][:2] == [("second", "user"), ("first", "user")]
    assert (
        await client.post("/api/items/memory/global/restore", json={"rev": revs[1]["rev"]})
    ).status_code == 204
    assert await _content(container, "global") == "first"

    await client.put("/api/items/memory/alpha", json={"content": "- likes tea\n"})
    assert (await client.get("/api/items/memory/alpha")).json()["owner"] == agent.id
    # 新建的归请求给出的 owner（缺省为平台），名字要是 slug
    created = await client.put("/api/items/memory/trading", json={"content": "x", "description": "交易"})
    assert (created.json()["owner"], created.json()["description"]) == (None, "交易")
    assert (await client.put("/api/items/memory/Bad Name", json={"content": "x"})).status_code == 422
    kinds = {i["name"]: i for i in (await client.get("/api/items")).json() if i["kind"] == "memory"}
    assert set(kinds) == {"alpha", "global", "trading"}
    assert kinds["global"]["roles"] == ["base"] and kinds["global"]["shared"]
    assert not kinds["trading"]["shared"]
    assert (await client.delete("/api/items/memory/trading")).status_code == 204
    assert (await client.delete("/api/items/memory/global")).status_code == 422
    assert (await client.get("/api/items/memory/nope")).status_code == 404
    assert (await client.get("/api/memory")).status_code == 404


async def test_a_private_memory_is_created_with_the_agent_and_keeps_its_name(container, make_agent):
    agent = await make_agent("alpha")
    assert await owned(container, "memory", agent.id) == ["alpha"]
    assert [r.message for r in await memory_history(container, "alpha")] == ["create memory"]
    # 改名不跟着改；占用这个名字的记忆在，就不能再建同名 agent
    await container.agents.update(agent.id, AgentPatch(name="renamed"), actor="user")
    assert await owned(container, "memory", agent.id) == ["alpha"]
    with pytest.raises(Conflict, match="memory named 'alpha'"):
        await container.agents.create(AgentSpec(name="alpha", description="d"), actor="user")


async def test_the_memory_limit_is_per_block_and_tells_how_much_to_free(container, make_agent):
    agent = await make_agent("alpha")
    with pytest.raises(MemoryFull, match="memory 'alpha' would be") as e:
        await container.memory.edit(
            "alpha", [TextChange("", "x" * (container.memory.max_chars + 1))], agent.id
        )
    assert agent.id not in str(e.value) and "free at least" in str(e.value)
    with pytest.raises(InvalidInput, match="limit"):
        await container.memory.put("global", "x" * (container.memory.max_chars + 1), "", actor="user")
    assert [r.message for r in await memory_history(container, "global")] == []


async def test_every_memory_write_tidies_whitespace_only(container, make_agent):
    # 行尾空白、连续空行、首尾空行在指令里看不出，memory_edit 对不上原文（#361）：每条写入路径都规整
    agent = await make_agent("alpha")
    await container.memory.put("alpha", "\n\n- a  \n\n\n\n  - b\t\n\n", "", actor="user")
    assert await _content(container, "alpha") == "- a\n\n  - b"
    await container.memory.edit("alpha", [TextChange("- a", "- a \n\n\n- c   ")], agent.id)
    assert await _content(container, "alpha") == "- a\n\n- c\n\n  - b"
    await container.memory.edit("alpha", [TextChange("", "- d\n\n\n")], agent.id)
    assert await _content(container, "alpha") == "- a\n\n- c\n\n  - b\n\n- d"
    await container.memory.create("trading", "x \n\n\n", actor="user")
    assert await _content(container, "trading") == "x"
    # 恢复：旧版本带着没规整的空白（#361 之前存下的），恢复出来的也规整
    async with container.db.transaction() as s:
        record_in(s, Subject.MEMORY, "alpha", None, "- old  \n\n\n", "user", "before #361")
    old = (await memory_history(container, "alpha"))[0]
    await container.memory.restore("alpha", old.id, actor="user")
    assert await _content(container, "alpha") == "- old"


async def test_the_memory_limit_counts_the_tidied_text(container, make_agent):
    agent = await make_agent("alpha")
    limit = container.memory.max_chars
    # 原文超过上限、规整后正好到上限：能写入
    await container.memory.put("alpha", "x" * (limit - 3) + "  \n\n\n", "", actor="user")
    await container.memory.edit("alpha", [TextChange("", "y")], agent.id)  # 追加出来的是 …\n\ny\n，比上限多 1
    assert await _content(container, "alpha") == "x" * (limit - 3) + "\n\ny"


async def test_several_memory_edits_check_the_limit_once_on_the_result(container, make_agent):
    # #375：一次调用里先加后删，中途超过上限不算；只看全部改完的结果
    agent = await make_agent("alpha")
    limit = container.memory.max_chars
    drop = "d" * 30 + "\n\n"
    await container.memory.put("alpha", drop + "x" * (limit - 40), "", actor="user")
    await container.memory.edit("alpha", [TextChange("", "y" * 20), TextChange(drop, "")], agent.id)
    assert await _content(container, "alpha") == "x" * (limit - 40) + "\n\n" + "y" * 20


async def test_a_later_failing_memory_edit_says_its_line_numbers_are_after_the_earlier_ones(
    container, make_agent
):
    # 前一处插了行，第 2 处的行号线索指改了一半的文本，附的全文是原文：报错要讲明，否则按行号会错位
    agent = await make_agent("alpha")
    await container.memory.put("alpha", "- a\n\n- b", "", actor="user")
    changes = [TextChange("- a", "- a\n\n- new"), TextChange("  - b", "- B")]
    with pytest.raises(InvalidInput) as e:
        await container.memory.edit("alpha", changes, agent.id)
    message = str(e.value)
    assert "edit 2 of 2" in message and "lines 5-5 match it except for whitespace" in message
    assert "refer to the text with the first 1 edit(s) applied" in message and "     3\t- b" in message
    assert await _content(container, "alpha") == "- a\n\n- b"


async def test_a_cleared_memory_is_a_new_version(container, make_agent):
    await make_agent("alpha")
    await container.memory.put("alpha", "- fact", "", actor="user")
    await container.memory.put("alpha", "", "", actor="user")
    assert await _content(container, "alpha") == ""
    assert len(await memory_history(container, "alpha")) == 3


async def test_who_can_edit_a_memory_block_follows_write_permissions(container, make_agent):
    alpha, beta = await make_agent("alpha"), await make_agent("beta")
    boss = await make_agent("boss", roles=("admin",))
    await container.memory.create("trading", "", actor="user")
    await container.memory.create("ops", "", actor="user")
    await container.distribution.create(RoleSpec("trader", "t", ["*:memory:trading"], []), actor="user")
    await container.distribution.create(RoleSpec("reader", "r", ["use:memory:ops"], []), actor="user")
    await container.agents.update(alpha.id, AgentPatch(roles=["trader", "reader"]), actor="user")
    # 自己的私有记忆、角色给了 write 的共享记忆能改
    await container.memory.edit("alpha", [TextChange("", "- mine")], alpha.id)
    await container.memory.edit("trading", [TextChange("", "- shared")], alpha.id)
    # 只给了 use 的、全局记忆、别人的私有记忆不能改
    for name in ("ops", "global", "beta"):
        with pytest.raises(Forbidden, match=f"write:memory:{name}"):
            await container.memory.edit(name, [TextChange("", "- no")], alpha.id)
    # admin 靠 write:* 能改任何一块，owner 不变
    await container.memory.edit("beta", [TextChange("", "- by admin")], boss.id)
    assert await owned(container, "memory", beta.id) == ["beta"]
    blocks = {m.name: m for m in await container.memory.for_run(alpha.id)}
    assert {n: (m.editable, m.yours) for n, m in blocks.items()} == {
        "alpha": (True, True),
        "global": (False, False),
        "ops": (False, False),
        "trading": (True, False),
    }
    # admin 能改不等于能用：别人的私有记忆不进它的指令
    assert "beta" not in {m.name for m in await container.memory.for_run(boss.id)}


async def test_an_agent_edits_a_memory_block_by_name_as_a_versioned_change(container, make_agent, models):
    agent = await make_agent("alpha", model="m", roles=["admin"])
    args = {
        "name": "global",
        "new_text": "- all agents answer in Chinese",
        "reason": "user asked twice for Chinese (session s1)",
    }
    models.models["m"] = call_then_answer("memory_edit", args, "Added a rule.")
    await start(container, agent.id, "hello")
    await drain(container)

    assert await _content(container, "global") == "- all agents answer in Chinese"
    assert await _content(container, "alpha") == ""
    latest = (await memory_history(container, "global"))[0]
    # 为什么改记进这一版：自进化的时间线
    assert (latest.actor, latest.message) == (agent.id, "user asked twice for Chinese (session s1)")

    args = {"name": "trading", "reason": "gather fee notes from cex-arb and wick-maker", "content": "- fees"}
    models.models["m"] = call_then_answer("memory_create", args, "Created.")
    await start(container, agent.id, "again")
    await drain(container)
    assert (await container.memory.read("trading")).owner is None


async def test_deleting_an_agent_removes_its_memory_unless_a_role_shares_it(container, make_agent):
    alpha, beta, other = await make_agent("alpha"), await make_agent("beta"), await make_agent("other")
    await container.memory.put("alpha", "- private", "", actor="user")
    await container.memory.put("beta", "- handed over", "", actor="user")
    await container.distribution.create(RoleSpec("b", "b", ["use:memory:beta"], []), actor="user")
    await container.agents.update(other.id, AgentPatch(roles=["b"]), actor="user")
    for a in (alpha, beta):
        await container.agent_lifecycle.delete(a.id, actor="user")
    await drain(container)

    with pytest.raises(NotFound):
        await container.memory.read("alpha")
    # 被删 agent 的记忆的历史仍在版本表里，可以审计
    assert len(await memory_history(container, "alpha")) == 3
    # 分给了别的 agent 的转给平台
    assert (await container.memory.read("beta")).owner is None
    assert "beta" in {m.name for m in await container.memory.for_run(other.id)}
