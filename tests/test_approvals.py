"""skill_run 审批（#530）：SKILL.md frontmatter 的 approval 列出的脚本，执行前停下等用户批准；批准后按原参数执行，
拒绝或没批准就继续时把说明交给模型；挂起期间重启照样等着，批准后执行中被打断不重跑。"""

import os

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo

from muliao.agents.skill_package import parse_frontmatter
from muliao.errors import InvalidInput
from muliao.platform.tables import Task, TaskStatus
from muliao.sessions.api import Origin
from muliao.sessions.approvals import DENIED, INTERRUPTED, NOT_DECIDED
from muliao.sessions.lanes import RUN_TASK
from muliao.workspace.grants import GRANTED_NOTE, ScriptCall
from tests.conftest import login, restart, share
from tests.scripted import scripted
from tests.test_flow import drain, start, tasks_of

SCRIPT = '#!/bin/sh\necho "$*" >> ran.txt\necho "deployed $*"\n'
GATED = {"skill": "ops", "script": "scripts/deploy.sh", "args": ["prod"], "timeout": 10}


def make_skill(container, owner: str) -> None:
    base = container.settings.assets_dir / owner / "skills" / "ops"
    (base / "scripts").mkdir(parents=True)
    (base / "SKILL.md").write_text(
        "---\nname: ops\ndescription: deploy things\napproval:\n  - ./scripts/deploy.sh\n---\n\nbody\n"
    )
    for name in ("deploy.sh", "status.sh"):
        path = base / "scripts" / name
        path.write_text(SCRIPT)
        os.chmod(path, 0o755)


class Gate:
    """第一次请求调用 skill_run（args 由测试定），之后记下模型看到的工具结果并收尾。"""

    def __init__(self, args: dict) -> None:
        self.args = args
        self.requests = 0
        self.results: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        last = messages[-1]
        if isinstance(last, ModelRequest):
            self.results += [
                p.model_response() if isinstance(p, RetryPromptPart) else p.model_response_str()
                for p in last.parts
                if isinstance(p, ToolReturnPart | RetryPromptPart)
            ]
        if self.requests == 1:
            return ModelResponse(parts=[ToolCallPart("skill_run", self.args, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("done")])


async def _gated(container, make_agent, models, args=GATED, origin=Origin.USER):
    agent = await make_agent("alpha", model="gate")
    make_skill(container, "alpha")
    gate = Gate(args)
    models.models["gate"] = scripted(gate)
    session = await start(container, agent.id, "deploy", origin=origin)
    await drain(container)
    ran = container.agents.workspace(agent.id) / "ran.txt"
    return session.id, gate, ran


def _approval_states(detail: dict) -> dict[str, str]:
    return {i["tool_call_id"]: i["approval"] for i in detail["items"] if i["approval"] is not None}


async def _decide(client, session_id: str, choice: bool | str) -> int:
    if isinstance(choice, bool):
        choice = "approve" if choice else "deny"
    resp = await client.post(
        f"/api/sessions/{session_id}/messages", json={"prompt": "", "approvals": {"c1": choice}}
    )
    return resp.status_code


async def test_a_gated_script_waits_for_approval_and_runs_once_approved(
    client, container, make_agent, models
):
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models)

    # 停在等批准上：脚本没跑，模型只被请求一次，会话在等，用户收到通知
    assert gate.requests == 1 and not ran.exists()
    [note] = (await client.get("/api/notifications")).json()
    assert "等你批准" in note["body"] and "scripts/deploy.sh" in note["body"]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert _approval_states(detail) == {"c1": "pending"}
    assert detail["state"] == "waiting"
    assert [(w["kind"], w["call_id"], w["tool_name"]) for w in detail["waits"]] == [
        ("approval", "c1", "skill_run")
    ]

    assert await _decide(client, session_id, True) == 202
    assert _approval_states((await client.get(f"/api/sessions/{session_id}")).json()) == {"c1": "submitted"}
    assert await _decide(client, session_id, True) == 409  # 已提交，不能再决定
    await drain(container)

    # 按原参数执行了一次，结果作为这次调用的结果交给模型
    assert ran.read_text() == "prod\n"
    assert gate.results == ["exit_code: 0\ndeployed prod\n"]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert _approval_states(detail) == {}
    assert detail["state"] == "idle"


async def test_a_denied_script_does_not_run_and_the_model_is_told(client, container, make_agent, models):
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models)

    assert await _decide(client, session_id, False) == 202
    await drain(container)

    assert not ran.exists()
    assert gate.results == [DENIED]


async def test_a_script_not_listed_runs_without_approval(client, container, make_agent, models):
    await login(client)
    args = {**GATED, "script": "scripts/status.sh"}
    session_id, gate, ran = await _gated(container, make_agent, models, args=args)

    assert ran.read_text() == "prod\n"
    assert gate.results == ["exit_code: 0\ndeployed prod\n"]
    assert all("等你批准" not in n["body"] for n in (await client.get("/api/notifications")).json())
    assert await _decide(client, session_id, True) == 409  # 没有在等批准的调用


async def test_a_call_with_bad_arguments_is_rejected_before_asking(client, container, make_agent, models):
    await login(client)
    args = {k: v for k, v in GATED.items() if k != "timeout"}
    session_id, gate, ran = await _gated(container, make_agent, models, args=args)

    # 参数不对（前台没给 timeout）当场交还模型，不去问用户
    assert gate.requests == 2 and "timeout is required" in gate.results[0]
    assert _approval_states((await client.get(f"/api/sessions/{session_id}")).json()) == {}


async def test_a_new_message_instead_of_a_decision_skips_the_call(client, container, make_agent, models):
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models)

    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "先别部署"})
    await drain(container)

    assert not ran.exists()
    assert gate.results == [NOT_DECIDED]
    # 调用已经结束：之后的决定没有对象
    assert await _decide(client, session_id, True) == 409


async def test_a_pending_approval_survives_a_restart(client, container, make_agent, models):
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models)

    await restart(container)
    await drain(container)
    # 重启后还在等批准，没有被当成「执行中被打断」
    assert _approval_states((await client.get(f"/api/sessions/{session_id}")).json()) == {"c1": "pending"}
    assert gate.requests == 1

    assert await _decide(client, session_id, True) == 202
    await drain(container)
    assert ran.read_text() == "prod\n"


async def test_an_approved_call_interrupted_while_running_is_not_run_again(
    client, container, make_agent, models
):
    """批准后，运行在执行脚本时进程退出（run task 留在 running）：恢复后不再执行，交给模型「被中断、可能已生效」
    的结果。"""
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models)
    assert await _decide(client, session_id, True) == 202
    await _crash_while_running(container)

    await restart(container)
    await drain(container)

    assert not ran.exists()
    assert gate.results == [INTERRUPTED]
    assert [t.status for t in await tasks_of(container, RUN_TASK)][-1] == TaskStatus.DONE


async def _crash_while_running(container) -> None:
    """模拟运行在执行批准过的脚本时进程退出：运行开头取走了信件、记下批准已交出（同一个事务），脚本还没出结果。"""
    from muliao.sessions.approvals import take_in

    run = (await tasks_of(container, RUN_TASK))[-1]
    async with container.db.transaction() as s:
        task = await s.get(Task, run.id)
        task.status, task.attempts = TaskStatus.RUNNING, 1
        assert await take_in(s, run.session_id, ["c1"]) == set()


@pytest.mark.parametrize("then", ["message", "retry"])
async def test_an_approval_taken_by_a_failed_run_is_not_run_again(
    client, container, make_agent, models, then
):
    """执行中途的运行最终失败了（换了 run task，尝试计数从头算）：之后新消息唤醒的运行、或手动重试，都不再执行。"""
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models)
    assert await _decide(client, session_id, True) == 202
    await _crash_while_running(container)
    run = (await tasks_of(container, RUN_TASK))[-1]
    async with container.db.transaction() as s:
        task = await s.get(Task, run.id)
        task.status, task.error = TaskStatus.FAILED, "boom"

    if then == "message":
        await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "怎么样了"})
    else:
        assert (await client.post(f"/api/tasks/{run.id}/retry")).status_code == 200
    await drain(container)

    assert not ran.exists()
    assert gate.results == [INTERRUPTED]


async def test_stopping_the_session_closes_the_pending_approval(client, container, make_agent, models):
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models)

    assert (await client.post(f"/api/sessions/{session_id}/stop")).status_code == 202
    await drain(container)

    assert not ran.exists()
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert _approval_states(detail) == {} and detail["state"] == "idle"
    assert await _decide(client, session_id, True) == 409


async def test_a_run_nobody_watches_still_notifies_the_user_to_approve(client, container, make_agent, models):
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models, origin=Origin.TRIGGER)

    [note] = (await client.get("/api/notifications")).json()
    assert "等你批准" in note["body"] and note["session_id"] == session_id


def test_the_approval_list_must_be_relative_script_paths():
    def fm(approval: str) -> str:
        return f"---\ndescription: d\napproval: {approval}\n---\nbody\n"

    assert parse_frontmatter(fm("[scripts/a.sh, b.py]"), "ops").approval == ("scripts/a.sh", "b.py")
    assert parse_frontmatter("---\ndescription: d\n---\n", "ops").approval == ()
    for bad in ("scripts/a.sh", "[/abs/a.sh]", "[1]", "['']"):
        with pytest.raises(InvalidInput, match="approval"):
            parse_frontmatter(fm(bad), "ops")


# ---- 始终允许（#531） ----


async def _again(container, models, agent_id: str, args=GATED, model: str = "gate2") -> tuple[str, Gate]:
    """同一个 agent 在新会话里再发起一次调用（模型换一个名字，各自记结果）。"""
    gate = Gate(args)
    models.models[model] = scripted(gate)
    from muliao.agents.service import AgentPatch

    await container.agents.update(agent_id, AgentPatch(model=model), actor="user")
    session = await start(container, agent_id, "again")
    await drain(container)
    return session.id, gate


async def _always_allowed(client, container, make_agent, models):
    session_id, gate, ran = await _gated(container, make_agent, models)
    assert await _decide(client, session_id, "always") == 202
    await drain(container)
    assert gate.results == ["exit_code: 0\ndeployed prod\n"]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    return detail["session"]["agent_id"], ran


async def test_an_always_allowed_call_runs_without_asking_next_time(client, container, make_agent, models):
    await login(client)
    agent_id, ran = await _always_allowed(client, container, make_agent, models)
    [grant] = (await client.get("/api/script-grants")).json()
    assert (
        grant["agent_id"],
        grant["skill"],
        grant["script"],
        grant["args"],
        grant["secrets"],
        grant["hits"],
    ) == (
        agent_id,
        "ops",
        "scripts/deploy.sh",
        ["prod"],
        [],
        0,
    )

    # 同一调用（timeout 不同也算同一个）：直接执行，结果里标明按始终允许放行，不再请用户批准
    session_id, gate = await _again(container, models, agent_id, args={**GATED, "timeout": 30})
    assert gate.results[0].startswith(GRANTED_NOTE) and "deployed prod" in gate.results[0]
    assert ran.read_text() == "prod\nprod\n"
    assert _approval_states((await client.get(f"/api/sessions/{session_id}")).json()) == {}
    assert sum("等你批准" in n["body"] for n in (await client.get("/api/notifications")).json()) == 1
    [grant] = (await client.get("/api/script-grants")).json()
    assert grant["hits"] == 1 and grant["last_used_at"] is not None


async def test_an_always_allowed_call_asks_again_when_anything_that_matters_changes(
    client, container, make_agent, models
):
    await login(client)
    agent_id, ran = await _always_allowed(client, container, make_agent, models)
    skill_dir = container.settings.assets_dir / "alpha" / "skills" / "ops"

    # 参数不同：要批准
    session_id, _ = await _again(container, models, agent_id, args={**GATED, "args": ["staging"]}, model="g1")
    assert _approval_states((await client.get(f"/api/sessions/{session_id}")).json()) == {"c1": "pending"}

    # 改了说明（SKILL.md）不算变：照样放行
    md = skill_dir / "SKILL.md"
    md.write_text(md.read_text() + "\nmore notes\n")
    _, gate = await _again(container, models, agent_id, model="g2")
    assert gate.results[0].startswith(GRANTED_NOTE)

    # 改了辅助文件：要批准
    (skill_dir / "scripts" / "lib.sh").write_text("helper\n")
    session_id, _ = await _again(container, models, agent_id, model="g3")
    assert _approval_states((await client.get(f"/api/sessions/{session_id}")).json()) == {"c1": "pending"}


async def test_a_revoked_grant_asks_again(client, container, make_agent, models):
    await login(client)
    agent_id, ran = await _always_allowed(client, container, make_agent, models)
    [grant] = (await client.get("/api/script-grants")).json()

    assert (await client.delete(f"/api/script-grants/{grant['id']}")).status_code == 204
    assert (await client.delete(f"/api/script-grants/{grant['id']}")).status_code == 404
    assert (await client.get("/api/script-grants")).json() == []
    session_id, _ = await _again(container, models, agent_id)
    assert _approval_states((await client.get(f"/api/sessions/{session_id}")).json()) == {"c1": "pending"}


async def test_a_grant_is_per_agent_and_goes_with_the_agent(client, container, make_agent, models):
    await login(client)
    await _always_allowed(client, container, make_agent, models)
    # 别的 agent 用同一个 skill（公共 skill）的同一调用：要批准
    other = await make_agent("beta", model="gate2")
    (container.settings.assets_dir / "platform" / "skills").mkdir(parents=True, exist_ok=True)
    (container.settings.assets_dir / "alpha" / "skills" / "ops").rename(
        container.settings.assets_dir / "platform" / "skills" / "ops"
    )
    await share(container, "use:skill:ops")
    session_id, _ = await _again(container, models, other.id)
    assert _approval_states((await client.get(f"/api/sessions/{session_id}")).json()) == {"c1": "pending"}

    alpha = (await client.get("/api/script-grants")).json()[0]["agent_id"]
    assert (await client.delete(f"/api/agents/{alpha}")).status_code == 204
    await drain(container)
    assert (await client.get("/api/script-grants")).json() == []


def test_a_script_call_key_ignores_the_order_of_secrets_but_not_their_set():
    def key(secrets: tuple[str, ...], agent: str = "a1") -> str:
        return ScriptCall.of(agent, "ops", "scripts/deploy.sh", ("prod",), secrets, "f" * 64).key

    assert key(("A", "B")) == key(("B", "A", "A"))
    assert key(("A",)) != key(("A", "B")) != key(())
    assert key(()) != key((), agent="a2")


def test_the_fingerprint_skips_skill_md_and_pycache_but_sees_helpers(tmp_path):
    from muliao.agents.skills import SkillScripts

    (tmp_path / "SKILL.md").write_text("a")
    (tmp_path / "run.py").write_text("print(1)")
    scripts = SkillScripts(tmp_path, ("run.py",))
    first = scripts.fingerprint()
    (tmp_path / "SKILL.md").write_text("b")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"x")
    assert scripts.fingerprint() == first
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "SKILL.md").write_text("nested is a helper")
    assert scripts.fingerprint() != first


def test_the_fingerprint_follows_a_symlinked_directory_and_survives_a_loop(tmp_path):
    from muliao.agents.skills import SkillScripts

    skill, outside = tmp_path / "skill", tmp_path / "outside"
    skill.mkdir()
    outside.mkdir()
    (skill / "run.sh").write_text("source lib/x.sh")
    (outside / "x.sh").write_text("v1")
    (skill / "lib").symlink_to(outside)
    (outside / "loop").symlink_to(skill)
    scripts = SkillScripts(skill, ("run.sh",))
    first = scripts.fingerprint()
    (outside / "x.sh").write_text("v2")
    assert scripts.fingerprint() != first
