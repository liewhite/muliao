"""运行历史只以库为准（#571）：收尾一个事务、悬空调用由平台补齐并落库（pydantic-ai 的修补无事可做）、每次运行一份配置
快照、开头的压缩只在有事交给模型时做。"""

import pytest
from pydantic_ai.messages import (
    SYNTHESIZED_TOOL_RETURN_METADATA_KEY,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import delete, select

from muliao.agent_runtime.runner import AgentRuntime, _unrepaired
from muliao.agents.api import AgentPatch
from muliao.platform.tables import Task, TaskStatus
from muliao.sessions import steps
from muliao.sessions.api import Origin
from muliao.sessions.compaction import COMPACTION_INSTRUCTIONS
from muliao.sessions.conventions import INTERRUPTED_TOOL_RESULT, unanswered_calls
from muliao.sessions.lanes import RUN_TASK, session_lane
from muliao.sessions.runs import RunPayload
from muliao.sessions.tables import ApprovalRequest
from tests.conftest import restart
from tests.scripted import scripted
from tests.test_approvals import GATED, make_skill
from tests.test_compaction import _session_with_exchanges
from tests.test_flow import drain, history_of, reply, start, tasks_of


class TwoCalls:
    """第一次请求：一个要批准的 skill_run（c1）和一个普通的（c2）；之后记下模型看到的历史并收尾。"""

    def __init__(self) -> None:
        self.seen: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(list(messages))
        if len(self.seen) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("skill_run", GATED, tool_call_id="c1"),
                    ToolCallPart("skill_run", {**GATED, "script": "scripts/status.sh"}, tool_call_id="c2"),
                ]
            )
        return ModelResponse(parts=[TextPart("done")])


def _results_after_calls(messages: list[ModelMessage]) -> dict[str, str]:
    """紧跟在发出 c1、c2 的那条回复之后的那条请求里的结果（模型看到的历史里请求已合并）。"""
    at = next(i for i, m in enumerate(messages) if isinstance(m, ModelResponse) and m.tool_calls)
    request = messages[at + 1]
    assert isinstance(request, ModelRequest)
    return {p.tool_call_id: p.content for p in request.parts if isinstance(p, ToolReturnPart)}


async def _db_unanswered(container, session_id: str) -> list[str]:
    return [c.tool_call_id for c in unanswered_calls(await history_of(container, session_id))]


async def test_a_paused_run_commits_its_tail_and_approval_request_in_one_transaction(
    container, make_agent, models, monkeypatch
):
    """停在等批准上的收尾：别的工具的结果与等批准的记录同一个事务。收尾失败时两样都没写，之后的运行给两个调用都补上
    「被中断」，结果紧跟在调用之后（以前结果先提交、记录没提交，等批准的调用永远没结果，下次运行把结果补在别的回复
    之后，提供方从此拒绝每一次请求）。"""
    agent = await make_agent("alpha", model="gate")
    make_skill(container, "alpha")
    script = TwoCalls()
    models.models["gate"] = scripted(script)

    async def broken(*_args: object) -> None:
        raise RuntimeError("process exits here")

    monkeypatch.setattr(steps, "record_approvals_in", broken)
    session = await start(container, agent.id, "deploy", origin=Origin.TRIGGER)
    await drain(container)
    monkeypatch.undo()

    history = await history_of(container, session.id)
    assert isinstance(history[-1], ModelResponse) and history[-1].tool_calls  # c2 的结果没有单独提交
    async with container.db.transaction() as s:
        assert (await s.scalars(select(ApprovalRequest))).all() == []

    await reply(container, session.id, "again")
    await drain(container)
    assert (await tasks_of(container, RUN_TASK))[-1].status == TaskStatus.DONE
    assert _results_after_calls(script.seen[-1]) == {
        "c1": INTERRUPTED_TOOL_RESULT,
        "c2": INTERRUPTED_TOOL_RESULT,
    }
    assert await _db_unanswered(container, session.id) == []


async def test_a_call_left_without_result_by_older_code_is_closed_in_the_database_where_it_belongs(
    container, make_agent, models
):
    """旧代码留下的样子：c2 的结果已提交、等批准的记录没提交，运行还记着 running。重启后平台把 c1 补成「被中断」并存库，
    交给模型的历史里它紧跟在调用之后；之后的运行照常（不会把结果补在后面的回复之后）。"""
    agent = await make_agent("alpha", model="gate")
    make_skill(container, "alpha")
    script = TwoCalls()
    models.models["gate"] = scripted(script)
    session = await start(container, agent.id, "deploy", origin=Origin.TRIGGER)
    await drain(container)
    assert len(script.seen) == 1
    run = (await tasks_of(container, RUN_TASK))[-1]
    async with container.db.transaction() as s:
        await s.execute(delete(ApprovalRequest))
        task = await s.get(Task, run.id)
        task.status, task.attempts = TaskStatus.RUNNING, 1

    await restart(container)
    await drain(container)

    assert (await tasks_of(container, RUN_TASK))[-1].status == TaskStatus.DONE
    results = _results_after_calls(script.seen[-1])
    assert results["c1"] == INTERRUPTED_TOOL_RESULT and "exit_code: 0" in results["c2"]
    assert await _db_unanswered(container, session.id) == []

    await reply(container, session.id, "again")
    await drain(container)
    assert (await tasks_of(container, RUN_TASK))[-1].status == TaskStatus.DONE
    assert len(script.seen) == 3


def test_a_call_without_result_before_the_last_reply_is_reported_not_answered_at_the_end():
    history = [
        ModelRequest(parts=[UserPromptPart("go")]),
        ModelResponse(parts=[ToolCallPart("run_shell", {}, tool_call_id="a")]),
        ModelRequest(parts=[UserPromptPart("more")]),
        ModelResponse(parts=[TextPart("done")]),
    ]
    with pytest.raises(RuntimeError, match=r"before its last response: \['a'\]"):
        unanswered_calls(history)
    # 最后一条回复里的没结果的调用是正常的（挂起，或这一步被打断）
    assert [c.tool_call_id for c in unanswered_calls(history[:2])] == ["a"]


def test_a_result_pydantic_ai_made_up_fails_the_run():
    repaired = ModelRequest(
        parts=[
            ToolReturnPart(
                "run_shell", "x", tool_call_id="a", metadata={SYNTHESIZED_TOOL_RETURN_METADATA_KEY: True}
            )
        ]
    )
    call = ModelResponse(parts=[ToolCallPart("run_shell", {}, tool_call_id="a")])
    with pytest.raises(RuntimeError, match="synthesized"):
        _unrepaired([call], [call, repaired])
    answered = ModelRequest(parts=[ToolReturnPart("run_shell", "x", tool_call_id="a")])
    with pytest.raises(RuntimeError, match=r"dropped tool results.*\['a'\]"):
        _unrepaired([answered], [ModelRequest(parts=[])])
    _unrepaired([call, answered], [call, answered])


async def test_a_run_keeps_its_model_when_agent_toml_changes_mid_run(
    container, make_agent, models, monkeypatch
):
    """运行中途（这里是步间压缩时）改了 agent 的模型：这次运行压缩后续跑、压缩本身都还用开头那份配置。"""
    monkeypatch.setattr(container.compactor, "_default_threshold", 1)
    monkeypatch.setattr(container.compactor, "_keep_steps", 1)
    agent = await make_agent("alpha", model="m")
    calls: list[str] = []

    def m(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.instructions == COMPACTION_INSTRUCTIONS:
            calls.append("m:compact")
            return ModelResponse(parts=[TextPart("SUMMARY")])
        calls.append("m")
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("history_append", {"content": "- x"}, tool_call_id="c")])
        return ModelResponse(parts=[TextPart("done")])

    def other(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append("other")
        return ModelResponse(parts=[TextPart("wrong model")])

    models.models["m"] = scripted(m)
    models.models["other"] = scripted(other)
    summarize = AgentRuntime.summarize

    async def switching(self, config, *args):  # noqa: ANN001, ANN002, ANN202
        await container.agents.update(agent.id, AgentPatch(model="other"), actor="user")
        return await summarize(self, config, *args)

    monkeypatch.setattr(AgentRuntime, "summarize", switching)
    session = await start(container, agent.id, "go", origin=Origin.TRIGGER)
    await drain(container)

    assert calls == ["m", "m:compact", "m"]
    assert (await history_of(container, session.id))[-1].parts == [TextPart("done")]


async def test_a_run_with_nothing_to_hand_the_model_does_not_compact(
    container, make_agent, models, monkeypatch
):
    agent = await make_agent("alpha", model="m")
    session, recorder = await _session_with_exchanges(container, models, agent.id, 3)
    monkeypatch.setattr(container.compactor, "_default_threshold", 1)
    monkeypatch.setattr(container.compactor, "_keep_steps", 1)
    async with container.db.transaction() as s:
        container.queue.enqueue(
            s,
            kind=RUN_TASK,
            lane=session_lane(session.id),
            agent_id=agent.id,
            session_id=session.id,
            payload=RunPayload(through_mail_id=0).model_dump(),
        )
    await drain(container)

    assert recorder.compactions == []
    assert (await tasks_of(container, RUN_TASK))[-1].status == TaskStatus.DONE
