"""agent_runtime.outcome：模型库的运行结果换成端口自己的结局（Finished / Paused）。"""

from types import SimpleNamespace

from pydantic_ai import DeferredToolRequests, ToolDenied
from pydantic_ai.messages import ToolCallPart

from muliao.agent_runtime.api import Approval, Finished, Paused
from muliao.agent_runtime.outcome import deferred_tool_results, outcome_of


def _result(output: object) -> SimpleNamespace:
    return SimpleNamespace(output=output, new_messages=lambda: [])


def test_a_text_output_is_a_finished_run():
    assert outcome_of(_result("done")) == Finished(answer="done", messages=[])  # type: ignore[arg-type]


def test_deferred_calls_are_a_paused_run():
    call = ToolCallPart(tool_name="ask_user", args={}, tool_call_id="c1")
    outcome = outcome_of(_result(DeferredToolRequests(calls=[call])))  # type: ignore[arg-type]
    assert outcome == Paused(calls=(call,))


def test_calls_asking_for_approval_are_a_paused_run():
    call = ToolCallPart(tool_name="skill_run", args={}, tool_call_id="c1")
    outcome = outcome_of(_result(DeferredToolRequests(approvals=[call])))  # type: ignore[arg-type]
    assert outcome == Paused(calls=(), approvals=(call,))


def test_decisions_on_approvals_go_to_approvals_and_other_results_to_calls():
    results = deferred_tool_results(
        {"a": Approval(approved=True), "d": Approval(approved=False, note="no"), "c": "answer"}
    )
    assert results.approvals == {"a": True, "d": ToolDenied("no")}
    assert results.calls == {"c": "answer"}
