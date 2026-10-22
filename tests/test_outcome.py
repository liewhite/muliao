"""agent_runtime.outcome：模型库的运行结果换成端口自己的结局（Finished / Paused）。"""

from types import SimpleNamespace

import pytest
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ToolCallPart

from muliao.agent_runtime.api import Finished, Paused
from muliao.agent_runtime.outcome import deferred_results, outcome_of


def _result(output: object) -> SimpleNamespace:
    return SimpleNamespace(output=output, new_messages=lambda: [])


def test_a_text_output_is_a_finished_run():
    assert outcome_of(_result("done")) == Finished(answer="done", messages=[])  # type: ignore[arg-type]


def test_deferred_calls_are_a_paused_run():
    call = ToolCallPart(tool_name="ask_user", args={}, tool_call_id="c1")
    outcome = outcome_of(_result(DeferredToolRequests(calls=[call])))  # type: ignore[arg-type]
    assert outcome == Paused(calls=(call,))


def test_a_run_asking_for_approval_is_rejected():
    call = ToolCallPart(tool_name="run_shell", args={}, tool_call_id="c1")
    with pytest.raises(RuntimeError, match="approvals.*run_shell"):
        outcome_of(_result(DeferredToolRequests(approvals=[call])))  # type: ignore[arg-type]


def test_results_are_handed_back_by_call_id():
    assert deferred_results(None) is None
    handed = deferred_results({"c1": "yes"})
    assert handed is not None and handed.calls == {"c1": "yes"}
