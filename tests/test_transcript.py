"""会话条目的投影：步骤时间（调用的开始、结果的结束；拿不到真实时间的为 None），调用关联的 thread。"""

from datetime import UTC, datetime, timedelta

from pydantic_ai.messages import ModelRequest, ModelResponse, ThinkingPart, ToolCallPart, ToolReturnPart

from muliao.platform.tool_views import NO_RENDERS
from muliao.sessions.history import interrupted_results, stopped_results
from muliao.sessions.transcript import annotate_threads, result_item, to_items

T0 = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
CALL = ToolCallPart("run_shell", {"command": "ls"}, tool_call_id="c")


def _result(at: datetime) -> ToolReturnPart:
    return ToolReturnPart("run_shell", "ok", tool_call_id="c", timestamp=at)


def test_a_call_starts_when_its_response_was_saved_and_ends_with_its_result():
    items = to_items([ModelResponse(parts=[CALL])], NO_RENDERS, saved_at=T0) + to_items(
        [ModelRequest(parts=[_result(T0 + timedelta(seconds=4))])], NO_RENDERS
    )
    assert [(i.type, i.at) for i in items] == [
        ("tool_call", T0),
        ("tool_result", T0 + timedelta(seconds=4)),
    ]


def test_without_a_save_time_a_call_has_no_start():
    assert to_items([ModelResponse(parts=[CALL])], NO_RENDERS)[0].at is None


def test_results_made_up_on_stop_or_interruption_have_no_end_time():
    # 补上的结果是停止、重启时才合成的，它的 timestamp 不是调用结束的时间
    for made_up in (stopped_results([CALL]), interrupted_results([CALL])):
        part = made_up[0].parts[0]
        assert isinstance(part, ToolReturnPart) and result_item(part, NO_RENDERS).at is None


ASK = ToolCallPart("ask_agent", {"agent": "a", "message": "q", "thread": "t"}, tool_call_id="ask")


def test_a_call_points_at_the_thread_it_opened():
    items = to_items([ModelResponse(parts=[ASK, CALL])], NO_RENDERS)
    assert [(i.tool_call_id, i.threads) for i in annotate_threads(items, {"ask": "th"})] == [
        ("ask", ["th"]),
        ("c", []),
    ]


def test_a_call_points_at_its_thread_whoever_made_up_its_result():
    # 停止、中断补上的结果不影响关联：只看调用 id
    history = [ModelResponse(parts=[ASK])]
    for made_up in (stopped_results([ASK]), interrupted_results([ASK])):
        items = to_items(history, NO_RENDERS) + to_items(made_up, NO_RENDERS)
        assert [(i.type, i.threads) for i in annotate_threads(items, {"ask": "th"})] == [
            ("tool_call", ["th"]),
            ("tool_result", []),
        ]


def test_the_live_snapshot_points_calls_at_their_threads():
    from muliao.sessions.live import LiveSegment, RunDeltaEvent, RunItemEvent
    from muliao.sessions.service import _with_threads

    [call_item] = to_items([ModelResponse(parts=[ASK])], NO_RENDERS)
    base = {"session_id": "s", "agent": "x", "segment_id": "r"}
    live = LiveSegment(
        "r",
        1,
        [
            RunDeltaEvent(**base, seq=0, item_type="thinking", text="hm"),
            RunItemEvent(**base, seq=1, item=call_item),
        ],
    )
    shown = _with_threads(live, {"ask": "th"})
    assert [e.item.threads for e in shown.events if isinstance(e, RunItemEvent)] == [["th"]]
    assert shown.events[0] == live.events[0] and live.events[1].item.threads == []


def test_thinking_shows_the_summary_or_else_the_raw_reasoning():
    """思考条目的文字（#611）：正文（摘要）；正文为空时是开源推理模型的原始推理文字，pydantic_ai 放在
    provider_details["raw_content"]；只有签名、没有文字的思考块不出条目。"""

    def shown(part: ThinkingPart) -> list[tuple[str, str]]:
        return [(i.type, i.text) for i in to_items([ModelResponse(parts=[part])], NO_RENDERS)]

    raw = {"raw_content": ["先看题目", "", "再算一遍"]}
    assert shown(ThinkingPart("", id="rs_1", provider_details=raw)) == [("thinking", "先看题目\n\n再算一遍")]
    assert shown(ThinkingPart("摘要", id="rs_1", provider_details=raw)) == [("thinking", "摘要")]
    assert shown(ThinkingPart("摘要", signature="sig")) == [("thinking", "摘要")]
    assert shown(ThinkingPart("", signature="sig", provider_details={"other": 1})) == []


def test_event_sources_are_parsed_once_for_the_ui():
    from muliao.sessions.sources import (
        JobSource,
        OtherSource,
        ThreadSource,
        job_holder,
        parse_source,
        thread_source,
    )

    assert parse_source(thread_source("abc-1")) == ThreadSource(
        kind="thread", raw="thread:abc-1", session_id="abc-1"
    )
    assert parse_source(job_holder(12)) == JobSource(kind="job", raw="job:12", job_id=12)
    assert parse_source("job:x") == OtherSource(kind="other", raw="job:x")
    assert parse_source("webhook") == OtherSource(kind="other", raw="webhook")
