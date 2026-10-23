"""用量：每次模型运行（对话、压缩）记一条，按时间范围与维度汇总。"""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from muliao.sessions.api import Origin
from muliao.sessions.compaction import COMPACTION_INSTRUCTIONS
from muliao.sessions.lanes import COMPACTION_TASK, RUN_TASK, session_lane
from muliao.usage.api import RunKind, UsageEntry, UsageGroup, UsageTotals
from muliao.usage.tables import ModelUsage
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, start, tasks_of

# 一次模型调用报告的用量：input 是输入总数，其中 80 命中缓存、10 写入缓存（pydantic-ai 的口径）。
# 只在非流式请求（压缩）里原样保留：会话的运行是流式的，FunctionModel 那时自己估算 token，
# 所以会话运行的测试只断言「记下来了」，确切数值由压缩那条覆盖
REPLY_USAGE = RequestUsage(input_tokens=100, output_tokens=5, cache_read_tokens=80, cache_write_tokens=10)


def answering(text: str = "ok"):
    """每次请求都直接回答，并报告固定用量。"""

    def fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)], usage=REPLY_USAGE)

    return scripted(fn)


async def usage_rows(container) -> list[ModelUsage]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(ModelUsage).order_by(ModelUsage.id))).all())


# ---- 记录 ----


async def test_a_turn_records_its_usage_against_its_agent_and_session(container, make_agent, models):
    alpha = await make_agent("alpha", model="m")
    models.models["m"] = answering()

    session = await start(container, alpha.id, "hi", origin=Origin.TRIGGER)
    await drain(container)

    [row] = await usage_rows(container)
    assert (row.kind, row.agent_id, row.session_id, row.model) == (RunKind.RUN, alpha.id, session.id, "m")
    assert row.requests == 1
    assert row.input_tokens > 0 and row.output_tokens > 0


async def test_compaction_is_billed_to_the_session_it_compacts(container, make_agent, models, monkeypatch):
    alpha = await make_agent("alpha", model="m")

    def fn(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        text = "SUMMARY" if info.instructions == COMPACTION_INSTRUCTIONS else "ok"
        return ModelResponse(parts=[TextPart(text)], usage=REPLY_USAGE)

    models.models["m"] = scripted(fn)
    session = await start(container, alpha.id, "hi", origin=Origin.TRIGGER)
    await drain(container)
    # 保留窗口设为 0，已有的两条消息就都可以压缩
    monkeypatch.setattr(container.compactor, "_keep_steps", 0)
    async with container.db.transaction() as s:
        container.queue.enqueue(
            s,
            kind=COMPACTION_TASK,
            lane=session_lane(session.id),
            agent_id=alpha.id,
            session_id=session.id,
            payload={},
        )
    await drain(container)

    [compaction] = [r for r in await usage_rows(container) if r.kind == RunKind.COMPACTION]
    assert (compaction.session_id, compaction.agent_id, compaction.model) == (session.id, alpha.id, "m")
    # 模型报告的四项计数原样入库（缓存读写是 input_tokens 的一部分）
    assert (compaction.requests, compaction.input_tokens, compaction.output_tokens) == (1, 100, 5)
    assert (compaction.cache_read_tokens, compaction.cache_write_tokens) == (80, 10)


async def test_a_run_that_fails_still_records_what_it_already_spent(container, make_agent, models):
    alpha = await make_agent("alpha", model="m")

    def fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if any(isinstance(m, ModelResponse) for m in messages):
            raise ModelHTTPError(500, "m", body="upstream is down")
        return ModelResponse(
            parts=[ToolCallPart("memory_append", {"entry": "- x"}, tool_call_id="c")], usage=REPLY_USAGE
        )

    models.models["m"] = scripted(fn)
    await start(container, alpha.id, "hi", origin=Origin.TRIGGER)
    await drain(container)

    rows = await usage_rows(container)
    # 暂时性失败按队列规则重试到用完次数；只有第一次尝试的那次请求真的花了 token（之后的尝试从已保存的回复续跑，
    # 请求一发就失败），所以只记一条
    [turn] = await tasks_of(container, RUN_TASK)
    assert turn.attempts == container.settings.runtime.model_retry_max_attempts
    assert len(rows) == 1 and all(
        r.kind == RunKind.RUN and r.requests == 1 and r.input_tokens > 0 for r in rows
    )


async def test_a_stopped_run_still_records_what_it_spent(client, container, make_agent, models):
    await login(client)
    alpha = await make_agent("alpha", model="slow")

    def fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("run_shell", {"command": "sleep 30"}, tool_call_id="slow")],
            usage=REPLY_USAGE,
        )

    models.models["slow"] = scripted(fn)
    session_id = (await client.post("/api/sessions", json={"agent_id": alpha.id, "prompt": "go"})).json()[
        "id"
    ]
    worker = asyncio.create_task(drain(container, limit_seconds=20))
    for _ in range(250):
        live = container.live.snapshot(session_id)
        if live is not None and any(e.event == "item" for e in live.events):
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the run never reached its tool call")
    assert (await client.post(f"/api/sessions/{session_id}/stop")).status_code == 202
    await worker

    # 被取消的运行同样花了 token：记账在屏蔽作用域里完成
    [row] = await usage_rows(container)
    assert (row.kind, row.session_id, row.requests) == (RunKind.RUN, session_id, 1)


async def test_a_failure_to_record_usage_does_not_fail_the_run(container, make_agent, models, monkeypatch):
    alpha = await make_agent("alpha", model="m")
    models.models["m"] = answering("still answered")

    async def boom(_entry: UsageEntry) -> None:
        raise RuntimeError("usage table is gone")

    monkeypatch.setattr(container.usage, "record", boom)
    session = await start(container, alpha.id, "hi", origin=Origin.TRIGGER)
    await drain(container)

    detail = await container.sessions.detail(session.id)
    assert [i.text for i in detail.items if i.type == "assistant"] == ["still answered"]


# ---- 汇总 ----


async def _record(container, when: datetime, *, agent: str, model: str, tokens: int) -> None:
    await container.usage.record(
        UsageEntry(
            agent_id=agent,
            session_id=None,
            kind=RunKind.RUN,
            model=model,
            totals=UsageTotals(
                requests=1,
                input_tokens=tokens,
                output_tokens=1,
                cache_read_tokens=tokens // 2,
                cache_write_tokens=0,
            ),
        )
    )
    # record 用的是默认的 created_at；汇总按时间分组，这里直接改成要的时刻
    async with container.db.transaction() as s:
        row = (await s.scalars(select(ModelUsage).order_by(ModelUsage.id.desc()).limit(1))).one()
        row.created_at = when


async def test_summary_groups_by_the_requested_dimensions(container):
    day = datetime(2026, 3, 1, 12, tzinfo=UTC)
    await _record(container, day, agent="a", model="m1", tokens=10)
    await _record(container, day, agent="a", model="m2", tokens=20)
    await _record(container, day + timedelta(days=1), agent="b", model="m1", tokens=30)
    window = (day - timedelta(days=1), day + timedelta(days=2))

    by_agent = await container.usage.summarize(*window, [UsageGroup.AGENT])
    assert [(b.agent_id, b.model, b.day, b.totals.input_tokens) for b in by_agent] == [
        ("a", None, None, 30),
        ("b", None, None, 30),
    ]
    by_model = await container.usage.summarize(*window, [UsageGroup.MODEL])
    assert [(b.model, b.totals.input_tokens, b.totals.requests) for b in by_model] == [
        ("m1", 40, 2),
        ("m2", 20, 1),
    ]
    total = await container.usage.summarize(*window, [])
    assert [(b.agent_id, b.model, b.day, b.totals.input_tokens) for b in total] == [(None, None, None, 60)]


async def test_days_follow_the_configured_timezone(container, settings, monkeypatch):
    # UTC 的 3 月 1 日 23:00 在东八区已经是 3 月 2 日
    monkeypatch.setattr(container.usage, "_tz", ZoneInfo("Asia/Shanghai"))
    await _record(container, datetime(2026, 3, 1, 23, tzinfo=UTC), agent="a", model="m", tokens=10)
    await _record(container, datetime(2026, 3, 2, 1, tzinfo=UTC), agent="a", model="m", tokens=20)

    buckets = await container.usage.summarize(
        datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC), [UsageGroup.DAY]
    )
    assert [(str(b.day), b.totals.input_tokens) for b in buckets] == [("2026-03-02", 30)]


async def test_summary_only_covers_the_requested_range(container):
    await _record(container, datetime(2026, 3, 1, tzinfo=UTC), agent="a", model="m", tokens=10)
    await _record(container, datetime(2026, 3, 5, tzinfo=UTC), agent="a", model="m", tokens=20)

    # 左闭右开
    buckets = await container.usage.summarize(
        datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 3, 5, tzinfo=UTC), []
    )
    assert [b.totals.input_tokens for b in buckets] == [10]


async def test_the_api_returns_buckets_for_the_requested_grouping(client, container):
    await login(client)
    await _record(container, datetime(2026, 3, 1, 12, tzinfo=UTC), agent="a", model="m", tokens=10)

    resp = await client.get(
        "/api/usage",
        params={
            "since": "2026-02-01T00:00:00Z",
            "until": "2026-04-01T00:00:00Z",
            "group_by": ["agent", "day"],
        },
    )
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "agent_id": "a",
            "model": None,
            "day": "2026-03-01",
            "requests": 1,
            "input_tokens": 10,
            "output_tokens": 1,
            "cache_read_tokens": 5,
            "cache_write_tokens": 0,
        }
    ]


async def test_the_api_rejects_a_time_without_a_timezone(client):
    await login(client)

    resp = await client.get(
        "/api/usage", params={"since": "2026-03-01T00:00:00", "until": "2026-03-02T00:00:00Z"}
    )

    # 不带时区的时刻没有确定含义：在边界上拒绝，而不是等到入库时炸在深处
    assert resp.status_code == 422 and "since" in str(resp.json()["detail"])


async def test_the_api_without_a_grouping_gives_one_total(client, container):
    await login(client)
    await _record(container, datetime(2026, 3, 1, tzinfo=UTC), agent="a", model="m", tokens=10)
    await _record(container, datetime(2026, 3, 2, tzinfo=UTC), agent="b", model="m", tokens=20)

    resp = await client.get(
        "/api/usage", params={"since": "2026-02-01T00:00:00Z", "until": "2026-04-01T00:00:00Z"}
    )
    assert [(b["agent_id"], b["model"], b["day"], b["input_tokens"]) for b in resp.json()] == [
        (None, None, None, 30)
    ]


def test_every_count_is_a_column_and_in_the_http_output():
    """计数只在 UsageTotals 里列出：表的列与接口的字段要与它同名，写入与输出才能直接从它推导。"""
    from muliao.api.usage import UsageOut
    from muliao.usage.service import COUNTS

    assert set(COUNTS) <= set(ModelUsage.__table__.columns.keys())
    assert set(COUNTS) <= set(UsageOut.model_fields)
