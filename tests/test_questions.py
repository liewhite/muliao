"""向用户提问（ask_user）：一轮停在提问上并通知用户，用户回答后作为工具结果继续；回答在服务端校验。"""

import asyncio
import json

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

from muliao.platform.tables import TaskStatus
from muliao.sessions.api import Origin
from muliao.sessions.ask_user import ASKED_BY_AGENT, NO_USER_WAITING
from muliao.sessions.codec import dump_message
from muliao.sessions.compaction import steps_of
from muliao.sessions.lanes import RUN_TASK
from muliao.sessions.tables import Message
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, history_of, start, tasks_of

QUESTIONS = [
    {
        "question": "看哪个周期？",
        "header": "周期",
        "options": [{"label": "日线"}, {"label": "4 小时"}],
    },
    {
        "question": "关注哪些指标？",
        "header": "指标",
        "options": [{"label": "成交量"}, {"label": "资金费率"}, {"label": "持仓量"}],
        "multi_select": True,
    },
]


def ask_states(detail: dict) -> dict[str, str]:
    """会话详情里每个 ask_user 调用的状态。"""
    return {i["tool_call_id"]: i["ask"]["status"] for i in detail["items"] if i["ask"] is not None}


def pending_asks(detail: dict) -> list[dict]:
    """会话详情里还在等用户回答的提问（条目上的 ask 状态）。"""
    return [
        {"tool_call_id": i["tool_call_id"], "questions": i["ask"]["questions"]}
        for i in detail["items"]
        if i["ask"] is not None and i["ask"]["status"] == "pending"
    ]


class Asker:
    """第一轮提问；之后记录模型看到的工具结果并作答。"""

    def __init__(self) -> None:
        self.requests = 0
        self.results: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        last = messages[-1]
        if isinstance(last, ModelRequest):
            self.results += [p.model_response_str() for p in last.parts if isinstance(p, ToolReturnPart)]
        if self.requests == 1:
            return ModelResponse(
                parts=[
                    TextPart("先确认两件事。"),
                    ToolCallPart("ask_user", {"questions": QUESTIONS}, tool_call_id="q1"),
                ]
            )
        return ModelResponse(parts=[TextPart("好的，开始分析。")])


async def _asked(client, container, make_agent, models) -> tuple[str, Asker]:
    await login(client)
    agent = await make_agent("analyst", model="asker")
    asker = Asker()
    models.models["asker"] = scripted(asker)
    resp = await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "分析 ETH"})
    session_id = resp.json()["id"]
    await drain(container)
    return session_id, asker


async def test_turn_stops_on_questions_and_answers_come_back_as_the_tool_result(
    client, container, make_agent, models
):
    session_id, asker = await _asked(client, container, make_agent, models)

    # 一轮在提问处结束：模型只被请求了一次，turn 完成，用户收到通知
    assert asker.requests == 1
    turns = await tasks_of(container, RUN_TASK)
    assert [t.status for t in turns] == [TaskStatus.DONE]
    [note] = (await client.get("/api/notifications")).json()
    assert "看哪个周期？" in note["body"] and note["session_id"] == session_id
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert [(p["tool_call_id"], len(p["questions"])) for p in pending_asks(detail)] == [("q1", 2)]
    # 停在提问上的会话在等用户回答：列表里是 waiting，不是空闲
    assert [s["state"] for s in (await client.get("/api/sessions")).json()] == ["waiting"]

    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量", "持仓量"], "other": "还有链上数据"}]}
    resp = await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    assert resp.status_code == 202 and resp.json()[0]["answers"]["q1"]["tool"] == "ask_user"
    assert resp.json()[0]["answers"]["q1"]["result"]["answers"][0]["selected"] == ["日线"]
    # 已提交、还没执行的回答不再显示成待回答的表单
    assert ask_states((await client.get(f"/api/sessions/{session_id}")).json()) == {"q1": "submitted"}
    await drain(container)

    assert json.loads(asker.results[0]) == {
        "answered": True,
        "answers": [
            {"question": "看哪个周期？", "selected": ["日线"], "other": None},
            {"question": "关注哪些指标？", "selected": ["成交量", "持仓量"], "other": "还有链上数据"},
        ],
        "note": None,
    }
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert ask_states(detail) == {"q1": "answered"}
    [ask] = [i["ask"] for i in detail["items"] if i["ask"] is not None]
    assert [a["selected"] for a in ask["answers"]] == [["日线"], ["成交量", "持仓量"]]
    assert detail["items"][-1]["text"] == "好的，开始分析。"
    assert [s["state"] for s in (await client.get("/api/sessions")).json()] == ["idle"]


async def test_answers_are_checked_against_the_pending_questions(client, container, make_agent, models):
    session_id, _ = await _asked(client, container, make_agent, models)
    url = f"/api/sessions/{session_id}/messages"

    async def post(answers):
        return (await client.post(url, json={"prompt": "", "answers": answers})).status_code

    assert await post({"q1": [{"selected": ["周线"]}, {"selected": ["成交量"]}]}) == 422  # 不是选项
    assert await post({"q1": [{"selected": ["日线", "4 小时"]}, {"selected": ["成交量"]}]}) == 422  # 单选
    assert await post({"q1": [{"selected": ["日线"]}, {"other": "  "}]}) == 422  # 没回答
    assert await post({"q1": [{"selected": ["日线"]}]}) == 422  # 少一题
    assert await post({"nope": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}) == 409  # 不在等回答
    ok = {"q1": [{"other": "周线"}, {"selected": ["资金费率"]}]}
    assert await post(ok) == 202
    assert await post(ok) == 409  # 重复提交


async def test_replying_without_answering_interrupts_the_questions(client, container, make_agent, models):
    session_id, asker = await _asked(client, container, make_agent, models)

    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "不用问了，直接分析"})
    await drain(container)

    assert [json.loads(r)["answered"] for r in asker.results] == [False]
    assert ask_states((await client.get(f"/api/sessions/{session_id}")).json()) == {"q1": "not_answered"}


async def test_an_answer_behind_a_queued_message_still_answers_the_questions(
    client, container, make_agent, models
):
    session_id, asker = await _asked(client, container, make_agent, models)
    # 消息还在信箱里排着、没交给模型：之后的回答照样作为提问的结果交回，消息接在后面
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "先看看别的"})
    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    resp = await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    assert resp.status_code == 202
    await drain(container)

    assert [json.loads(r)["answered"] for r in asker.results] == [True]
    history = await history_of(container, session_id)
    assert "先看看别的" in str(history[-2].parts[-1].content)


def test_an_answer_arriving_after_the_questions_closed_reaches_the_model_as_text():
    """回答投信后、交给模型前，提问已被别的运行结束（它带着「没回答」继续了）：回答作为一段文字交给模型。"""
    from muliao.sessions.pending import UserAnswered, late_text
    from muliao.sessions.questions import AnsweredQuestion, AskResult

    answered = AskResult(
        answered=True,
        answers=[AnsweredQuestion(question="看哪个周期？", selected=["日线"], other="也看周线")],
        note=None,
    )
    text = late_text([UserAnswered.of(answered)])
    assert "already been closed" in text and "看哪个周期？: 日线; 也看周线" in text


async def test_a_failed_answer_holds_the_questions_until_it_is_retried(client, container, make_agent, models):
    session_id, asker = await _asked(client, container, make_agent, models)
    original = models.models["asker"]

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model is down")

    models.models["asker"] = scripted(broken)
    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    await drain(container)
    failed = (await tasks_of(container, RUN_TASK))[-1]
    assert failed.status == TaskStatus.FAILED

    # 失败的回答还占着这次提问：不显示表单，不能再答，只能重试
    assert pending_asks((await client.get(f"/api/sessions/{session_id}")).json()) == []
    resp = await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    assert resp.status_code == 409
    models.models["asker"] = original
    assert (await client.post(f"/api/tasks/{failed.id}/retry")).status_code == 200
    await drain(container)
    assert json.loads(asker.results[-1])["answered"] is True


async def test_rejected_question_arguments_do_not_leave_a_pending_question(
    client, container, make_agent, models
):
    await login(client)
    agent = await make_agent("analyst", model="sloppy")
    calls = 0

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:  # 题数不合法：被工具 schema 拒绝，交还模型重试
            return ModelResponse(parts=[ToolCallPart("ask_user", {"questions": []}, tool_call_id="bad")])
        if calls == 2:
            return ModelResponse(
                parts=[ToolCallPart("ask_user", {"questions": QUESTIONS[:1]}, tool_call_id="good")]
            )
        return ModelResponse(parts=[TextPart("继续")])

    models.models["sloppy"] = scripted(fn)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "go"})).json()[
        "id"
    ]
    await drain(container)

    detail = await client.get(f"/api/sessions/{session_id}")
    assert detail.status_code == 200
    assert [p["tool_call_id"] for p in pending_asks(detail.json())] == ["good"]
    # 参数被拒绝的调用不是提问：按普通工具调用展示
    assert ask_states(detail.json()) == {"good": "pending"}
    resp = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"prompt": "", "answers": {"good": [{"selected": ["日线"]}]}},
    )
    assert resp.status_code == 202
    await drain(container)
    assert [t.status for t in await tasks_of(container, RUN_TASK)] == [TaskStatus.DONE, TaskStatus.DONE]


async def test_ask_user_is_always_listed_so_the_tool_list_stays_cacheable(container, make_agent, models):
    """工具列表在缓存前缀最前面：有没有用户在等，列出的工具都一样。"""
    agent = await make_agent("alpha", model="m")
    seen: list[list[str]] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append([t.name for t in info.function_tools])
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, agent.id, "hi", origin=Origin.USER)
    await start(container, agent.id, "event", origin=Origin.TRIGGER)
    await drain(container)

    assert "ask_user" in seen[0] and seen[0] == seen[1]


def test_compaction_never_separates_a_question_from_its_answer():
    def row(i: int, task_id: int, message: ModelMessage) -> Message:
        return Message(id=i, session_id="s", task_id=task_id, payload=dump_message(message))

    ask = ModelResponse(parts=[ToolCallPart("ask_user", {"questions": QUESTIONS}, tool_call_id="q1")])
    answer = ModelRequest(parts=[ToolReturnPart("ask_user", "[]", tool_call_id="q1")])
    done = ModelResponse(parts=[TextPart("ok")])
    rows = [row(1, 10, ask), row(2, 11, answer), row(3, 11, done), row(4, 12, done)]

    assert [[r.id for r in step] for step in steps_of(rows)] == [[1, 2], [3], [4]]


async def test_a_later_run_finishes_a_failed_answer_and_retrying_it_does_nothing(
    client, container, make_agent, models
):
    """回答交给模型后运行失败；之后的消息唤醒的运行从失败处续跑（回答不丢、推送由它做），再重试失败的运行什么都不做。"""
    session_id, asker = await _asked(client, container, make_agent, models)
    original = models.models["asker"]

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model is down")

    models.models["asker"] = scripted(broken)
    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    await drain(container)
    failed = (await tasks_of(container, RUN_TASK))[-1]
    assert failed.status == TaskStatus.FAILED

    models.models["asker"] = original
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "算了，先聊别的"})
    await drain(container)
    assert [json.loads(r)["answered"] for r in asker.results] == [True]
    notes = (await client.get("/api/notifications")).json()
    # 提问、运行失败、接手的运行回答了回答、之后的消息
    assert len(notes) == 4 and any(n["title"].endswith("运行失败") for n in notes)

    before = await history_of(container, session_id)
    assert (await client.post(f"/api/tasks/{failed.id}/retry")).status_code == 200
    await drain(container)
    retried = next(t for t in await tasks_of(container, RUN_TASK) if t.id == failed.id)
    assert retried.status == TaskStatus.DONE
    assert await history_of(container, session_id) == before
    assert len((await client.get("/api/notifications")).json()) == 4


async def test_a_voided_failed_answer_does_not_block_later_questions(client, container, make_agent, models):
    """失败的回答被之后的轮次作废后，不能永远挡住这个会话之后的提问。"""
    session_id, asker = await _asked(client, container, make_agent, models)
    original = models.models["asker"]

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model is down")

    models.models["asker"] = scripted(broken)
    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    await drain(container)

    # 之后的轮次关闭了 q1，又问了一次（Asker 第一次请求之外都回答文字，这里换一个会再问的模型）
    asked = 0

    def ask_again(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal asked
        asked += 1
        return ModelResponse(
            parts=[ToolCallPart("ask_user", {"questions": QUESTIONS}, tool_call_id=f"q2-{asked}")]
        )

    models.models["asker"] = scripted(ask_again)
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "换个问题"})
    await drain(container)

    pending = pending_asks((await client.get(f"/api/sessions/{session_id}")).json())
    [latest] = [p["tool_call_id"] for p in pending]
    models.models["asker"] = original
    answers = {latest: [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    resp = await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    assert resp.status_code == 202


async def test_a_question_when_no_user_waits_is_rejected_with_the_reason(
    client, container, make_agent, models
):
    """参数合法、但没有用户在等这次运行的回答：调用被拒绝并交还模型（说明原因），它不是提问。"""
    await login(client)
    agent = await make_agent("analyst", model="blind")

    retries: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart("ask_user", {"questions": QUESTIONS[:1]}, tool_call_id="q")]
            )
        retries.extend(str(p.content) for p in messages[-1].parts if isinstance(p, RetryPromptPart))
        return ModelResponse(parts=[TextPart("那我自己决定")])

    models.models["blind"] = scripted(fn)
    session_id = (await start(container, agent.id, "event", origin=Origin.TRIGGER)).id
    await drain(container)

    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert ask_states(detail) == {}
    assert [i["type"] for i in detail["items"] if i["tool_call_id"] == "q"] == ["tool_call", "tool_error"]
    assert retries == [NO_USER_WAITING]


async def test_a_question_in_a_private_thread_is_handed_back_to_the_asking_agent(
    client, container, make_agent, models
):
    """私有 thread 里是另一个 agent 在问：拒绝时让它把问题写进回答交回发起方，不建议去通知用户。"""
    from tests.test_flow import tool_returns

    await login(client)
    lead = await make_agent("lead", model="l")
    await make_agent("helper", model="h")
    retries: list[str] = []

    def lead_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if tool_returns(messages):
            return ModelResponse(parts=[TextPart("ok")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "ask_agent",
                    {"agent": "helper", "message": "x", "thread": "h", "timeout": 600},
                    tool_call_id="a",
                )
            ]
        )

    def helper_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart("ask_user", {"questions": QUESTIONS[:1]}, tool_call_id="q")]
            )
        retries.extend(str(p.content) for p in messages[-1].parts if isinstance(p, RetryPromptPart))
        return ModelResponse(parts=[TextPart("请你定：看哪个周期？")])

    models.models["l"] = scripted(lead_fn)
    models.models["h"] = scripted(helper_fn)
    await client.post("/api/sessions", json={"agent_id": lead.id, "prompt": "go"})
    await drain(container)

    assert retries == [ASKED_BY_AGENT]
    assert "notify_user" not in ASKED_BY_AGENT


async def test_a_live_question_is_shown_as_asking(container, make_agent, models):
    """运行中的提问条目与历史同一个投影：还不能回答（asking）。"""
    agent = await make_agent("analyst", model="asker")
    models.models["asker"] = scripted(Asker())
    events = []

    async def collect():
        async with container.bus.subscribe() as sub:
            async for e in sub:
                events.append(e)

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await start(container, agent.id, "分析", origin=Origin.USER)
    await drain(container)
    collector.cancel()
    [ask] = [e.item.ask for e in events if e.type == "run" and e.event == "item" and e.item.ask is not None]
    assert ask.status == "asking" and len(ask.questions) == 2


async def test_an_answered_question_is_not_replayed_as_a_live_item(client, container, make_agent, models):
    """续跑时 pydantic-ai 会把交回结果的挂起调用再发一遍事件：它已经在历史里，实时条目里不再出现。"""
    session_id, _ = await _asked(client, container, make_agent, models)
    events = []

    async def collect():
        async with container.bus.subscribe() as sub:
            async for e in sub:
                events.append(e)

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)
    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    await drain(container)
    collector.cancel()

    items = [e.item for e in events if e.type == "run" and e.event == "item"]
    assert all(i.tool_call_id != "q1" for i in items)
    assert any(e.type == "run" and e.event == "delta" for e in events)


async def test_two_questions_wait_for_both_answers_and_a_message_skips_the_rest(
    client, container, make_agent, models
):
    """同一次运行里问了两次：两次都回答了才继续（sessions.pending.waiting）；只回答了一次就发消息，另一次是被跳过，
    不是「发了新消息」。"""
    await login(client)
    agent = await make_agent("analyst", model="twice")
    results: list[dict] = []

    def twice(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest) and (
            returns := [p for p in last.parts if isinstance(p, ToolReturnPart)]
        ):
            results.extend(json.loads(p.model_response_str()) for p in returns)
            return ModelResponse(parts=[TextPart("好")])
        return ModelResponse(
            parts=[
                ToolCallPart("ask_user", {"questions": QUESTIONS[:1]}, tool_call_id="a"),
                ToolCallPart("ask_user", {"questions": QUESTIONS[1:]}, tool_call_id="b"),
            ]
        )

    models.models["twice"] = scripted(twice)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "分析"})).json()[
        "id"
    ]
    await drain(container)
    resp = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"prompt": "", "answers": {"a": [{"selected": ["日线"]}]}},
    )
    assert resp.status_code == 202
    await drain(container)
    # 还有一次提问没回答：等着
    assert results == []

    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "另一个先不答"})
    await drain(container)
    answered, skipped = results
    assert answered["answered"] is True
    assert skipped["answered"] is False and "new message" not in skipped["note"]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert ask_states(detail) == {"a": "answered", "b": "not_answered"}
    [note] = [i["ask"]["note"] for i in detail["items"] if i["tool_call_id"] == "b" and i["ask"] is not None]
    assert note == skipped["note"]


async def test_a_question_asked_alongside_another_tool_is_answered_as_its_result(
    client, container, make_agent, models
):
    """同一个回复里既提问又调了别的工具：挂起结束时别的工具的结果已经追加成一条请求。回答要作为提问的结果交回，
    不能把那条请求当作被打断的请求重发（重发会让提问没有结果，真实提供方拒绝这样的请求）。"""
    await login(client)
    agent = await make_agent("analyst", model="both")
    results: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(m, ModelResponse) for m in messages):
            return ModelResponse(
                parts=[
                    ToolCallPart("ask_user", {"questions": QUESTIONS}, tool_call_id="q1"),
                    ToolCallPart("memory_edit", {"reason": "r", "new_text": "- asked"}, tool_call_id="m1"),
                ]
            )
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        results.extend(
            (p.tool_name, p.model_response_str()) for p in last.parts if isinstance(p, ToolReturnPart)
        )
        return ModelResponse(parts=[TextPart("好的")])

    models.models["both"] = scripted(fn)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "分析"})).json()[
        "id"
    ]
    await drain(container)
    assert [
        p["tool_call_id"] for p in pending_asks((await client.get(f"/api/sessions/{session_id}")).json())
    ] == ["q1"]

    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    await drain(container)

    assert [t.status for t in await tasks_of(container, RUN_TASK)] == [TaskStatus.DONE, TaskStatus.DONE]
    # 发给模型时 pydantic-ai 把相邻的两条请求合成一条：别的工具的结果与提问的结果在一起
    assert [name for name, _ in results] == ["memory_edit", "ask_user"]
    assert json.loads(results[1][1])["answered"] is True
    assert await container.memory.read(agent.id) == "- asked\n"  # 别的工具只执行了一次


def test_an_answer_for_another_tool_than_the_pending_call_is_an_error():
    import pytest
    from pydantic_ai.messages import ToolCallPart

    from muliao.sessions.pending import AgentAnswer, AgentAnswered, deferred_results

    call = ToolCallPart("ask_user", {"questions": []}, tool_call_id="c")
    wrong = AgentAnswered.of(AgentAnswer(text="hi", attachments=[]))
    with pytest.raises(RuntimeError, match="got an answer for ask_agent"):
        deferred_results({"c": call}, {"c": wrong})


def test_an_answer_mailed_before_thread_calls_still_parses():
    # #251 之前的回答信件里带 thread 键：现在 thread 记在 thread_calls，旧键忽略
    from muliao.sessions.pending import AgentAnswer, AgentAnswered

    parsed = AgentAnswered.model_validate(
        {"tool": "ask_agent", "result": {"text": "hi", "attachments": [], "thread": "t"}}
    )
    assert parsed.result == AgentAnswer(text="hi", attachments=[])
