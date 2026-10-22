"""会话条目上的步骤时间：调用的开始、结果的结束；拿不到真实时间的为 None。"""

from datetime import UTC, datetime, timedelta

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from muliao.sessions.history import interrupted_results, stopped_results
from muliao.sessions.transcript import checked_times, result_item, to_items

T0 = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
CALL = ToolCallPart("run_shell", {"command": "ls"}, tool_call_id="c")


def _result(at: datetime) -> ToolReturnPart:
    return ToolReturnPart("run_shell", "ok", tool_call_id="c", timestamp=at)


def test_a_call_starts_when_its_response_was_saved_and_ends_with_its_result():
    items = to_items([ModelResponse(parts=[CALL])], saved_at=T0) + to_items(
        [ModelRequest(parts=[_result(T0 + timedelta(seconds=4))])]
    )
    assert [(i.type, i.at) for i in checked_times(items)] == [
        ("tool_call", T0),
        ("tool_result", T0 + timedelta(seconds=4)),
    ]


def test_without_a_save_time_a_call_has_no_start():
    assert to_items([ModelResponse(parts=[CALL])])[0].at is None


def test_results_made_up_on_stop_or_interruption_have_no_end_time():
    # 补上的结果是停止、重启时才合成的，它的 timestamp 不是调用结束的时间
    history = [ModelResponse(parts=[CALL])]
    for made_up in (stopped_results(history), interrupted_results([CALL])):
        part = made_up[0].parts[0]
        assert isinstance(part, ToolReturnPart) and result_item(part).at is None


def test_a_start_after_the_result_is_dropped_as_untrusted():
    # 10-20 之前一整轮在结束时才一次提交：回复那一行的保存时间晚于它的工具结果
    items = to_items([ModelResponse(parts=[CALL])], saved_at=T0 + timedelta(minutes=3)) + to_items(
        [ModelRequest(parts=[_result(T0)])]
    )
    assert [i.at for i in checked_times(items)] == [None, T0]
