"""工作历史：只追加、按时间与关键词检索、工具只写自己的。"""

import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from muliao.errors import InvalidInput
from muliao.history.service import HistoryEntry, HistoryLog
from tests.test_tools import run_tools


def entry(day: str, title: str, summary: str = "做了点事", refs: tuple[str, ...] = (), importance: int = 1):
    return HistoryEntry(date.fromisoformat(day), title, summary, refs, importance)


@pytest.fixture
def history(container) -> HistoryLog:
    return container.history


async def test_listing_without_query_is_newest_day_first_and_filters_by_agent_range_and_importance(history):
    await history.append("a1", [entry("2026-10-20", "旧事"), entry("2026-10-21", "要紧事", importance=3)])
    await history.append("a1", [entry("2026-10-21", "同一天后写的")])
    await history.append("a2", [entry("2026-10-22", "别人的事")])

    mine = await history.search(agent_ids=["a1"])
    assert [r.title for r in mine] == ["同一天后写的", "要紧事", "旧事"]
    assert [r.title for r in await history.search(agent_ids=None, limit=2)] == ["别人的事", "同一天后写的"]
    ranged = await history.search(agent_ids=None, since=date(2026, 10, 21), until=date(2026, 10, 21))
    assert {r.title for r in ranged} == {"同一天后写的", "要紧事"}
    assert [r.title for r in await history.search(agent_ids=["a1"], min_importance=2)] == ["要紧事"]


async def test_query_uses_trigram_full_text_for_chinese_and_refs(history):
    await history.append(
        "a1",
        [
            entry("2026-10-20", "修好触发器的前置检查超时", "precheck 超过 60 秒被杀", refs=("#120",)),
            entry("2026-10-21", "部署 muliao", "装了新版本", refs=("#155", "abc1234")),
            entry("2026-10-22", "写早报", "列出停滞的 issue"),
        ],
    )
    assert [r.title for r in await history.search(agent_ids=None, query="前置检查")] == [
        "修好触发器的前置检查超时"
    ]
    assert [r.title for r in await history.search(agent_ids=None, query="#155")] == ["部署 muliao"]
    # 每个词都要出现
    assert await history.search(agent_ids=None, query="前置检查 部署") == []
    assert [r.title for r in await history.search(agent_ids=None, query="PRECHECK 60 秒")] == [
        "修好触发器的前置检查超时"
    ]
    # 双引号不会破坏 MATCH 表达式
    assert await history.search(agent_ids=None, query='"早报"') == []


async def test_short_words_fall_back_to_like(history):
    await history.append("a1", [entry("2026-10-20", "写早报"), entry("2026-10-21", "写周报", summary="50%")])
    assert [r.title for r in await history.search(agent_ids=None, query="早报")] == ["写早报"]
    assert [r.title for r in await history.search(agent_ids=None, query="写")] == ["写周报", "写早报"]
    # LIKE 的通配符按字面匹配
    assert [r.title for r in await history.search(agent_ids=None, query="0%")] == ["写周报"]
    assert await history.search(agent_ids=None, query="_") == []


async def test_rows_cannot_be_updated_or_deleted(container, history):
    await history.append("a1", [entry("2026-10-20", "一件事")])
    for sql in ("UPDATE agent_history SET title = 'x'", "DELETE FROM agent_history"):
        with pytest.raises(IntegrityError, match="append-only"):
            async with container.db.transaction() as s:
                await s.execute(text(sql))
    assert [r.title for r in await history.search(agent_ids=None)] == ["一件事"]


async def test_entries_are_checked_before_anything_is_written(container):
    history = HistoryLog(
        container.db, ZoneInfo("Asia/Shanghai"), now=lambda: datetime(2026, 10, 22, 17, tzinfo=UTC)
    )
    # UTC 还是 10-22，配置的时区下已经是 10-23
    (today,) = await history.append("a1", [entry("2026-10-23", "今天的事")])
    assert today.created_at == datetime(2026, 10, 22, 17, tzinfo=UTC)
    bad = [
        entry("2026-10-24", "明天的事"),
        entry("2026-10-20", " "),
        entry("2026-10-20", "x" * 201),
        entry("2026-10-20", "没摘要", summary=" \n"),
        entry("2026-10-20", "太重要", importance=4),
    ]
    for e in bad:
        with pytest.raises(InvalidInput):
            await history.append("a1", [entry("2026-10-20", "合法的"), e])
    with pytest.raises(InvalidInput):
        await history.append("a1", [])
    assert [r.title for r in await history.search(agent_ids=None)] == ["今天的事"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 201},
        {"min_importance": 0},
        {"query": "  "},
        {"since": date(2026, 10, 2), "until": date(2026, 10, 1)},
    ],
)
async def test_search_rejects_invalid_arguments(history, kwargs):
    with pytest.raises(InvalidInput):
        await history.search(agent_ids=None, **kwargs)


async def test_tools_append_to_own_history_and_search_by_agent_name(container, make_agent, models):
    other = await make_agent("other")
    await container.history.append(other.id, [entry("2026-10-19", "别人做的部署")])
    me = await make_agent("me", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (
                "history_append",
                {
                    "entries": [
                        {
                            "day": "2026-10-20",
                            "title": "我做的部署",
                            "summary": "装好了",
                            "refs": ["#1"],
                            "importance": 2,
                        }
                    ]
                },
            ),
            ("history_search", {"query": "部署"}),
            ("history_search", {"agent": "other"}),
            ("history_search", {"agent": "all", "query": "的部署"}),
            ("history_append", {"entries": [{"day": "2999-01-01", "title": "未来", "summary": "x"}]}),
        ],
    )
    assert "appended 1 entries" in outputs[0]
    mine = await container.history.search(agent_ids=[me.id])
    assert [(r.title, r.refs, r.importance) for r in mine] == [("我做的部署", ("#1",), 2)]
    assert "我做的部署" in outputs[1] and "别人做的部署" not in outputs[1]
    assert [(e["title"], e["agent"]) for e in json.loads(outputs[2])] == [("别人做的部署", "other")]
    assert "我做的部署" in outputs[3] and "别人做的部署" in outputs[3]
    assert "day is after today" in outputs[4]


async def test_http_lists_one_agents_history(container, make_agent, client):
    from tests.conftest import login

    a = await make_agent("a")
    await container.history.append(a.id, [entry("2026-10-20", "旧"), entry("2026-10-21", "新", refs=("#1",))])
    await container.history.append("someone-else", [entry("2026-10-21", "别人的")])
    await login(client)
    resp = await client.get(f"/api/agents/{a.id}/history")
    assert resp.status_code == 200
    assert [(h["day"], h["title"], h["refs"]) for h in resp.json()] == [
        ("2026-10-21", "新", ["#1"]),
        ("2026-10-20", "旧", []),
    ]
    assert [
        h["title"] for h in (await client.get(f"/api/agents/{a.id}/history", params={"query": "旧"})).json()
    ] == ["旧"]
    assert (await client.get(f"/api/agents/{a.id}/history", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/agents/nope/history")).status_code == 404


async def test_refs_match_by_their_values_not_their_json_text(history):
    await history.append("a1", [entry("2026-10-20", "评审", summary="开会", refs=("设计评审会", "#7"))])
    await history.append("a1", [entry("2026-10-21", "别的", summary="无关")])
    assert [r.title for r in await history.search(agent_ids=None, query="设计评审")] == ["评审"]
    assert [r.title for r in await history.search(agent_ids=None, query="评审会")] == ["评审"]
    assert [r.title for r in await history.search(agent_ids=None, query="计评")] == ["评审"]
    for noise in ("[", '"', ",", "u8bbe"):
        assert await history.search(agent_ids=None, query=noise) == []
