"""会话事件队列：事件等下一轮一起处理；会话等用户回答提问时留着；处理事件的那一轮失败不会无限重新投递。"""

import asyncio

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.models import Origin, ReplyTo, SessionEvent, TaskKind, TaskStatus
from muliao.runtime.events import post_in
from muliao.transcript import to_items
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, start, tasks_of
from tests.test_questions import QUESTIONS


class Recorder:
    """记下每次请求里最后一条用户消息的内容段；可选地在第一次请求时停在提问上。"""

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
    container, session_id: str, content: str, reply_to: ReplyTo = ReplyTo.NONE, source: str = "job:1"
):
    async with container.db.transaction() as s:
        return await post_in(
            s, container.queue, session_id, source=source, content=content, reply_to=reply_to
        )


async def queued(container, session_id: str) -> list[str]:
    async with container.db.transaction() as s:
        rows = await s.scalars(
            select(SessionEvent.content).where(
                SessionEvent.session_id == session_id, SessionEvent.consumed_by.is_(None)
            )
        )
        return list(rows.all())


async def test_an_event_to_an_idle_session_is_handled_in_a_turn_of_its_own(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    recorder = Recorder()
    models.models["rec"] = scripted(recorder)
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    await drain(container)

    await post(container, session.id, "job 1 finished: exit 0")
    await drain(container)

    assert recorder.prompts[-1] == ['<event source="job:1">\njob 1 finished: exit 0\n</event>']
    assert await queued(container, session.id) == []
    # 界面上事件单独成条
    history = await container.sessions.history(session.id)
    events = [i for i in to_items(history) if i["type"] == "event"]
    assert [(i["tool_name"], i["text"]) for i in events] == [("job:1", "job 1 finished: exit 0")]


async def test_events_wait_for_an_answer_and_ride_along_with_it(client, container, make_agent, models):
    await login(client)
    agent = await make_agent("alpha", model="rec")
    recorder = Recorder(ask_first=True)
    models.models["rec"] = scripted(recorder)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "分析"})).json()[
        "id"
    ]
    await drain(container)

    # 等用户回答时事件留在队列里，不开新的一轮（否则会把提问记成没回答）
    await post(container, session_id, "check: still running")
    await drain(container)
    assert len(await tasks_of(container, TaskKind.TURN)) == 1
    assert await queued(container, session_id) == ["check: still running"]

    answers = {"q1": [{"selected": ["日线"]}, {"selected": ["成交量"]}]}
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "", "answers": answers})
    await drain(container)

    assert recorder.prompts[-1] == ['<event source="job:1">\ncheck: still running\n</event>']
    assert await queued(container, session_id) == []


async def test_events_arriving_during_a_turn_are_handled_right_after_it(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    session = await start(container, agent.id, "first", origin=Origin.TRIGGER)
    recorder = Recorder()
    posted: list[asyncio.Task] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not posted:
            # 这一轮正在运行时到达的事件
            posted.append(asyncio.get_running_loop().create_task(post(container, session.id, "late")))
        return recorder(messages, info)

    models.models["rec"] = scripted(fn)
    await drain(container)
    await asyncio.gather(*posted)
    await drain(container)

    assert recorder.prompts == [["first"], ['<event source="job:1">\nlate\n</event>']]
    assert await queued(container, session.id) == []


async def test_a_failed_event_turn_is_not_redispatched_forever(container, make_agent, models):
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
    assert (await tasks_of(container, TaskKind.TURN))[-1].status == TaskStatus.FAILED

    # 事件已经交给了那一轮：对账不重复投递
    assert await container.events.reconcile() == 0
    # 之后到达的事件开新的一轮，并带上之前那条
    models.models["rec"] = scripted(recorder)
    await post(container, session.id, "second")
    await drain(container)
    assert [len(p) for p in recorder.prompts[-1:]] == [2]
    assert await queued(container, session.id) == []


async def test_reconcile_dispatches_an_event_that_was_never_handed_to_a_turn(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    models.models["rec"] = scripted(Recorder())
    await drain(container)
    # 绕过 post_in 直接写入：如同投递那一刻进程死掉，事件在、轮次没有
    async with container.db.transaction() as s:
        s.add(SessionEvent(session_id=session.id, source="job:1", content="orphan", reply_to=ReplyTo.NONE))

    assert await container.events.reconcile() == 1
    await drain(container)
    assert await queued(container, session.id) == []


async def test_an_event_asking_to_reach_the_user_pushes_the_answer(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    models.models["rec"] = scripted(Recorder())
    await drain(container)

    await post(container, session.id, "check", ReplyTo.NONE)
    await drain(container)
    assert await tasks_of(container, TaskKind.DELIVER) == []

    await post(container, session.id, "job finished", ReplyTo.USER)
    await drain(container)
    assert len(await tasks_of(container, TaskKind.DELIVER)) == 1


async def test_events_are_ordered_around_the_user_message_by_arrival(container, make_agent, models):
    agent = await make_agent("alpha", model="rec")
    recorder = Recorder()
    models.models["rec"] = scripted(recorder)
    session = await start(container, agent.id, "hi", origin=Origin.TRIGGER)
    await drain(container)
    # 先到的事件让会话开了一轮；那一轮还没执行时用户发了消息，又来一条事件
    await post(container, session.id, "early")
    from tests.test_flow import reply

    await reply(container, session.id, "user says")
    await post(container, session.id, "later")
    await drain(container)

    contents = [c for p in recorder.prompts[1:] for c in p]
    assert contents.index('<event source="job:1">\nearly\n</event>') < contents.index("user says")
    assert await queued(container, session.id) == []


def test_a_stopped_turn_keeps_its_events_marked_on_the_saved_prompt():
    """停在第一次请求之前：补上的开头请求带着事件段，标记打在它身上（界面仍把事件单独显示）。"""
    from muliao.conventions import TURN_EVENTS, format_event, mark_events
    from muliao.runtime.stopping import stopped_messages

    prompt = [format_event("job:1", "done"), "and the user says hi"]
    messages = mark_events(stopped_messages(None, prompt, None), [0])

    [request] = messages
    assert request.metadata[TURN_EVENTS] == [0]
    assert [(i["type"], i["text"]) for i in to_items(messages)] == [
        ("event", "done"),
        ("user", "and the user says hi"),
        ("stopped", ""),
    ]


def test_events_read_as_events_in_transcripts_for_reflection_and_compaction():
    from muliao.conventions import format_event, mark_events
    from muliao.transcript import to_text

    request = ModelRequest(parts=[UserPromptPart([format_event("job:7", "exit 0"), "thanks"])])
    text = to_text(to_items(mark_events([request], [0])), max_item_chars=1000)
    assert text.splitlines() == ["[EVENT job:7] exit 0", "[USER] thanks"]
