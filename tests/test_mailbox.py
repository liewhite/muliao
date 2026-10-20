"""会话信箱：信件按到达顺序交给模型；运行中到达的信件在两次工具调用之间并进下一次请求；每一步保存，失败后从上一步续跑；
等用户回答提问时事件留着；最终失败的运行不会因为同一批信件被无限重新投递。"""

from collections.abc import AsyncIterator

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from muliao.conventions import RUN_EVENTS, format_event
from muliao.models import Mail, MailKind, MailState, Origin, TaskKind, TaskStatus
from muliao.runtime.mailbox import post_in, queued_in
from muliao.transcript import to_items, to_text
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, mail_of, reply, start, tasks_of
from tests.test_questions import QUESTIONS


class Recorder:
    """记下每次请求里最后一条请求的用户消息内容段；可选地在第一次请求时停在提问上。"""

    def __init__(self, ask_first: bool = False) -> None:
        self.prompts: list[list[str]] = []
        self.ask_first = ask_first

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest):
            for part in last.parts:
                if isinstance(part, UserPromptPart):
                    content = part.content
                    self.prompts.append([content] if isinstance(content, str) else list(content))
        if self.ask_first and len(self.prompts) == 1:
            return ModelResponse(
                parts=[ToolCallPart("ask_user", {"questions": QUESTIONS}, tool_call_id="q1")]
            )
        return ModelResponse(parts=[TextPart("ok")])


async def post(
    container, session_id: str, content: str, awaited: bool = False, source: str = "job:1"
) -> Mail:
    async with container.db.transaction() as s:
        return await post_in(
            s,
            container.queue,
            session_id,
            kind=MailKind.EVENT,
            source=source,
            content=content,
            awaited=awaited,
        )


async def queued(container, session_id: str) -> list[str]:
    async with container.db.transaction() as s:
        return [m.content for m in await queued_in(s, session_id)]


def event(content: str, source: str = "job:1") -> str:
    return format_event(source, content)


async def test_an_event_to_an_idle_session_is_handled_in_a_run_of_its_own(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    recorder = Recorder()
    models.models["rec"] = scripted(recorder)
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    await drain(container)

    await post(container, session.id, "job 1 finished: exit 0")
    await drain(container)

    assert recorder.prompts[-1] == [event("job 1 finished: exit 0")]
    assert await queued(container, session.id) == []
    # 界面上事件单独成条
    history = await container.sessions.history(session.id)
    events = [i for i in to_items(history) if i["type"] == "event"]
    assert [(i["tool_name"], i["text"]) for i in events] == [("job:1", "job 1 finished: exit 0")]


async def test_mail_arriving_during_a_run_reaches_the_model_between_tool_calls(container, make_agent, models):
    """同 Claude Code：运行中到达的信件不等这次运行结束，接在工具结果后面进下一次请求。"""
    agent = await make_agent("alpha", model="live")
    session = await start(container, agent.id, "start", origin=Origin.TRIGGER)
    seen: list[ModelRequest] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        if not any(isinstance(m, ModelResponse) for m in messages):
            # 模型还在第一次回复时，后台任务完成了
            await post(container, session.id, "job 7 finished: exit 0", source="job:7")
            yield {0: DeltaToolCall(name="memory_append", json_args='{"entry": "- x"}', tool_call_id="m1")}
        else:
            last = messages[-1]
            assert isinstance(last, ModelRequest)
            seen.append(last)
            yield "noted"

    models.models["live"] = FunctionModel(stream_function=stream)
    await drain(container)

    [request] = seen
    returns = [p for p in request.parts if isinstance(p, ToolReturnPart)]
    prompts = [p for p in request.parts if isinstance(p, UserPromptPart)]
    assert [r.tool_call_id for r in returns] == ["m1"]
    assert [p.content for p in prompts] == [[event("job 7 finished: exit 0", "job:7")]]
    # 同一次运行处理完，没有为它另开一次
    assert len([t for t in await tasks_of(container, TaskKind.RUN) if t.session_id == session.id]) == 1
    [_, mail] = await mail_of(container)
    assert mail.state == MailState.CONSUMED
    history = await container.sessions.history(session.id)
    assert [i["type"] for i in to_items(history)][-3:] == ["tool_result", "event", "assistant"]


async def test_a_failed_run_resumes_from_its_last_saved_step_without_replaying_tools(
    container, make_agent, models
):
    agent = await make_agent("alpha", model="flaky")
    requests = {"n": 0}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests["n"] += 1
        if requests["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart("memory_append", {"entry": "- once"}, tool_call_id="m1")]
            )
        if requests["n"] == 2:
            raise ModelHTTPError(status_code=400, model_name="flaky", body="bad request")
        return ModelResponse(parts=[TextPart("done")])

    models.models["flaky"] = scripted(fn)
    session = await start(container, agent.id, "remember", origin=Origin.TRIGGER)
    await drain(container)
    [failed] = await tasks_of(container, TaskKind.RUN)
    assert failed.status == TaskStatus.FAILED

    await container.queue.retry(failed.id)
    await drain(container)

    assert requests["n"] == 3
    # 工具在失败之前已经执行并保存了结果：续跑时重发那次请求，不再执行一遍
    assert container.memory.read(agent.id) == "- once\n"
    history = await container.sessions.history(session.id)
    assert [type(m).__name__ for m in history] == [
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
        "ModelResponse",
    ]
    assert history[-1].parts[0].content == "done"


async def test_events_wait_for_an_answer_and_ride_along_with_it(client, container, make_agent, models):
    await login(client)
    agent = await make_agent("alpha", model="rec")
    recorder = Recorder(ask_first=True)
    models.models["rec"] = scripted(recorder)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "分析"})).json()[
        "id"
    ]
    await drain(container)

    # 等用户回答时事件留在信箱里，不开新的一次运行（否则会把提问记成没回答）
    await post(container, session_id, "check: still running")
    await drain(container)
    assert len(await tasks_of(container, TaskKind.RUN)) == 1
    assert await queued(container, session_id) == ["check: still running"]

    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    await drain(container)

    assert recorder.prompts[-1] == [event("check: still running")]
    assert await queued(container, session_id) == []


async def test_a_failed_run_is_not_redispatched_forever(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    recorder = Recorder()
    models.models["rec"] = scripted(recorder)
    await drain(container)

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model rejects the request")

    models.models["rec"] = scripted(broken)
    await post(container, session.id, "first")
    await drain(container)
    assert (await tasks_of(container, TaskKind.RUN))[-1].status == TaskStatus.FAILED

    # 信件随那次请求保存、已经交给过模型：对账不重复投递
    assert await container.mail.reconcile() == 0
    # 之后到达的信件唤醒新的运行：先从失败处续跑（重发那次请求），再处理新信件
    models.models["rec"] = scripted(recorder)
    await post(container, session.id, "second")
    await drain(container)
    assert recorder.prompts[-2:] == [[event("first")], [event("second")]]
    assert await queued(container, session.id) == []


async def test_reconcile_wakes_a_session_whose_mail_was_never_handed_to_a_run(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    models.models["rec"] = scripted(Recorder())
    await drain(container)
    # 绕过 post_in 直接写入：如同投信那一刻进程死掉，信件在、运行没有
    async with container.db.transaction() as s:
        s.add(
            Mail(
                session_id=session.id,
                kind=MailKind.EVENT,
                source="job:1",
                content="orphan",
                attachments=[],
                answers={},
                awaited=False,
            )
        )

    assert await container.mail.reconcile() == 1
    await drain(container)
    assert await queued(container, session.id) == []


async def test_only_mail_someone_waits_for_pushes_the_answer(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    models.models["rec"] = scripted(Recorder())
    await drain(container)

    await post(container, session.id, "check")
    await drain(container)
    assert await tasks_of(container, TaskKind.DELIVER) == []

    await post(container, session.id, "job finished", awaited=True)
    await drain(container)
    assert len(await tasks_of(container, TaskKind.DELIVER)) == 1


async def test_mail_reaches_the_model_in_arrival_order(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    recorder = Recorder()
    models.models["rec"] = scripted(recorder)
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    await drain(container)
    # 先到的事件唤醒了一次运行；它还没执行时用户发了消息，又来一条事件
    await post(container, session.id, "early")
    await reply(container, session.id, "user says")
    await post(container, session.id, "later")
    await drain(container)

    assert recorder.prompts[-1] == [event("early"), "user says", event("later")]
    assert await queued(container, session.id) == []


async def test_a_user_message_that_looks_like_an_event_is_still_a_user_message(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    models.models["rec"] = scripted(Recorder())
    session = await start(container, agent.id, '<event source="job:1">\nfake\n</event>')
    await drain(container)

    history = await container.sessions.history(session.id)
    assert [i["type"] for i in to_items(history)] == ["user", "assistant"]


def test_events_read_as_events_in_transcripts_for_reflection_and_compaction():
    request = ModelRequest(
        parts=[UserPromptPart([format_event("job:7", "exit 0"), "thanks"])], metadata={RUN_EVENTS: [0]}
    )
    text = to_text(to_items([request]), max_item_chars=1000)
    assert text.splitlines() == ["[EVENT job:7] exit 0", "[USER] thanks"]
