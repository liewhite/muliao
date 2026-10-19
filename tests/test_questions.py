"""向用户提问（ask_user）：一轮停在提问上并通知用户，用户回答后作为工具结果继续；回答在服务端校验。"""

import json

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo

from muliao.models import Message, Origin, TaskKind, TaskStatus
from muliao.runtime.codec import dump_message
from muliao.runtime.compaction import _group_turns
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, start, tasks_of

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
    turns = await tasks_of(container, TaskKind.TURN)
    assert [t.status for t in turns] == [TaskStatus.DONE]
    deliver = await tasks_of(container, TaskKind.DELIVER)
    assert "看哪个周期？" in deliver[0].payload["body"]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert [(p["tool_call_id"], len(p["questions"])) for p in detail["pending_questions"]] == [("q1", 2)]

    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量", "持仓量"], "other": "还有链上数据"}]}
    resp = await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    assert resp.status_code == 202 and resp.json()["answers"]["q1"]["answers"][0]["selected"] == ["日线"]
    # 已提交、还没执行的回答不再显示成待回答的表单
    assert (await client.get(f"/api/sessions/{session_id}")).json()["pending_questions"] == []
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
    assert detail["pending_questions"] == []
    assert detail["items"][-1]["text"] == "好的，开始分析。"


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
    assert (await client.get(f"/api/sessions/{session_id}")).json()["pending_questions"] == []


async def test_questions_cannot_be_answered_behind_a_queued_message(client, container, make_agent, models):
    session_id, asker = await _asked(client, container, make_agent, models)
    # 先发的消息会先执行并把提问记为没回答，之后的回答已经没有意义
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "先看看别的"})

    assert (await client.get(f"/api/sessions/{session_id}")).json()["pending_questions"] == []
    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    resp = await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    assert resp.status_code == 409
    await drain(container)
    assert [json.loads(r)["answered"] for r in asker.results] == [False]


async def test_a_failed_answer_holds_the_questions_until_it_is_retried(client, container, make_agent, models):
    session_id, asker = await _asked(client, container, make_agent, models)
    original = models.models["asker"]

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model is down")

    models.models["asker"] = scripted(broken)
    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    await drain(container)
    failed = (await tasks_of(container, TaskKind.TURN))[-1]
    assert failed.status == TaskStatus.FAILED

    # 失败的回答还占着这次提问：不显示表单，不能再答，只能重试
    assert (await client.get(f"/api/sessions/{session_id}")).json()["pending_questions"] == []
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
    assert [p["tool_call_id"] for p in detail.json()["pending_questions"]] == ["good"]
    resp = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"prompt": "", "answers": {"good": [{"selected": ["日线"]}]}},
    )
    assert resp.status_code == 202
    await drain(container)
    assert [t.status for t in await tasks_of(container, TaskKind.TURN)] == [TaskStatus.DONE, TaskStatus.DONE]


async def test_ask_user_is_offered_only_when_the_user_reads_the_answer(container, make_agent, models):
    agent = await make_agent("alpha", model="m")
    seen: list[list[str]] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append([t.name for t in info.function_tools])
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, agent.id, "hi", origin=Origin.USER)
    await start(container, agent.id, "event", origin=Origin.TRIGGER)
    await drain(container)

    assert "ask_user" in seen[0] and "ask_user" not in seen[1]


def test_compaction_never_separates_a_question_from_its_answer():
    def row(i: int, task_id: int, message: ModelMessage) -> Message:
        return Message(id=i, session_id="s", task_id=task_id, payload=dump_message(message))

    ask = ModelResponse(parts=[ToolCallPart("ask_user", {"questions": QUESTIONS}, tool_call_id="q1")])
    answer = ModelRequest(parts=[ToolReturnPart("ask_user", "[]", tool_call_id="q1")])
    done = ModelResponse(parts=[TextPart("ok")])
    rows = [row(1, 10, ask), row(2, 11, answer), row(3, 11, done), row(4, 12, done)]

    assert [[r.id for r in turn] for turn in _group_turns(rows)] == [[1, 2, 3], [4]]
