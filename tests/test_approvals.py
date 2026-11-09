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
from tests.conftest import login, restart
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


async def _decide(client, session_id: str, approved: bool) -> int:
    resp = await client.post(
        f"/api/sessions/{session_id}/messages", json={"prompt": "", "approvals": {"c1": approved}}
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
    """批准后，运行在执行脚本时进程退出（run task 留在 running、已经尝试过一次）：恢复后不再执行，交给模型
    「被中断、可能已生效」的结果。"""
    await login(client)
    session_id, gate, ran = await _gated(container, make_agent, models)
    assert await _decide(client, session_id, True) == 202
    run = (await tasks_of(container, RUN_TASK))[-1]
    async with container.db.transaction() as s:
        task = await s.get(Task, run.id)
        task.status, task.attempts = TaskStatus.RUNNING, 1

    await restart(container)
    await drain(container)

    assert not ran.exists()
    assert gate.results == [INTERRUPTED]
    assert [t.status for t in await tasks_of(container, RUN_TASK)][-1] == TaskStatus.DONE


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
