"""会话条目的投影：步骤时间（调用的开始、结果的结束；拿不到真实时间的为 None），挂起调用关联的 thread。"""

from datetime import UTC, datetime, timedelta

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from muliao.sessions.history import interrupted_results, stopped_results
from muliao.sessions.transcript import annotate_pending_threads, checked_times, result_item, to_items

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


ASK = ToolCallPart("ask_agent", {"agent": "a", "message": "q", "thread": "t"}, tool_call_id="ask")


def test_a_call_still_waiting_points_at_the_thread_it_handed_over():
    items = to_items([ModelResponse(parts=[ASK, CALL])])
    assert [(i.tool_call_id, i.threads) for i in annotate_pending_threads(items, {"ask": "th"})] == [
        ("ask", ["th"]),
        ("c", []),
    ]


def test_a_stopped_call_does_not_point_at_its_thread():
    # 停止补上的结果不带 thread：那个 thread 列为未关联，不由占用补回
    history = [ModelResponse(parts=[ASK])]
    items = to_items(history) + to_items(stopped_results(history))
    assert [i.threads for i in annotate_pending_threads(items, {"ask": "th"})] == [[], []]


def test_the_live_snapshot_points_pending_calls_at_their_threads():
    from muliao.sessions.live import LiveRun, RunDeltaEvent, RunItemEvent
    from muliao.sessions.service import _with_pending_threads

    [call_item] = to_items([ModelResponse(parts=[ASK])])
    base = {"session_id": "s", "agent": "x", "run_id": "r"}
    live = LiveRun(
        "r",
        1,
        [
            RunDeltaEvent(**base, seq=0, item_type="thinking", text="hm"),
            RunItemEvent(**base, seq=1, item=call_item),
        ],
    )
    shown = _with_pending_threads(live, {"ask": "th"})
    assert [e.item.threads for e in shown.events if isinstance(e, RunItemEvent)] == [["th"]]
    assert shown.events[0] == live.events[0] and live.events[1].item.threads == []


def test_event_sources_are_parsed_once_for_the_ui():
    from muliao.sessions.sources import (
        JobSource,
        OtherSource,
        ThreadSource,
        job_source,
        parse_source,
        thread_source,
    )

    assert parse_source(thread_source("abc-1")) == ThreadSource(
        kind="thread", raw="thread:abc-1", session_id="abc-1"
    )
    assert parse_source(job_source(12)) == JobSource(kind="job", raw="job:12", job_id=12)
    assert parse_source("job:x") == OtherSource(kind="other", raw="job:x")
    assert parse_source("webhook") == OtherSource(kind="other", raw="webhook")
