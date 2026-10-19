"""端到端：触发 → 任务队列 → agent 运行 → 持久化 → 反思 / 通知 / 委派。"""

import asyncio
import re

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
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from muliao.models import Origin, Reflection, ReplyTo, Task, TaskKind, TaskStatus
from muliao.runtime.conversation import Inbound, NewSession
from tests.scripted import scripted


async def drain(container, limit_seconds: float = 10.0) -> None:
    worker = asyncio.create_task(container.worker.run_forever())
    try:
        async with asyncio.timeout(limit_seconds):
            while True:
                await asyncio.sleep(0.02)
                async with container.db.transaction() as s:
                    busy = await s.scalar(
                        select(Task.id)
                        .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
                        .limit(1)
                    )
                if busy is None:
                    return
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def last_user_prompt(messages: list[ModelMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart):
                    return str(part.content)
    raise AssertionError("no user prompt")


def tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    last = messages[-1]
    return [p for p in last.parts if isinstance(p, ToolReturnPart)] if isinstance(last, ModelRequest) else []


def call_then_answer(tool: str, args: dict, answer: str) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if tool_returns(messages):
            return ModelResponse(parts=[TextPart(answer)])
        return ModelResponse(parts=[ToolCallPart(tool, args)])

    return scripted(fn)


_REPLY_TO = {Origin.USER: ReplyTo.USER, Origin.TRIGGER: ReplyTo.NONE}


async def start(container, agent: str, prompt: str, origin: Origin = Origin.USER):
    received = await container.sessions.receive(
        Inbound(
            origin=origin,
            origin_ref=None,
            content=prompt,
            session=NewSession(agent_id=agent, title=prompt[:20]),
            reply_to=_REPLY_TO[origin],
        )
    )
    return received.session


async def reply(container, session_id: str, prompt: str):
    """用户在会话里追问，返回这一轮的 task。"""
    received = await container.sessions.receive(
        Inbound(
            origin=Origin.USER, origin_ref=None, content=prompt, session=session_id, reply_to=ReplyTo.USER
        )
    )
    return received.turn


async def tasks_of(container, kind: TaskKind) -> list[Task]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Task).where(Task.kind == kind).order_by(Task.id))).all())


async def test_turn_runs_tools_persists_messages_and_queues_notification(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    models.models["m"] = call_then_answer("memory_append", {"entry": "- user likes tea"}, "Noted!")

    session = await start(container, alpha, "I like tea")
    await drain(container)

    detail = await container.sessions.get(session.id)
    history = await container.sessions.history(session.id)
    assert detail.unread
    assert container.memory.read(alpha) == "- user likes tea\n"
    assert last_user_prompt(history) == "I like tea"
    assert isinstance(history[-1], ModelResponse) and history[-1].parts[0].content == "Noted!"
    deliver = await tasks_of(container, TaskKind.DELIVER)
    assert len(deliver) == 1 and deliver[0].status == TaskStatus.DONE
    assert deliver[0].payload["body"] == "Noted!"


async def test_triggered_session_does_not_notify_automatically(container, make_agent):
    alpha = (await make_agent("alpha")).id
    await start(container, alpha, "hi", origin=Origin.TRIGGER)
    await drain(container)

    assert await tasks_of(container, TaskKind.DELIVER) == []


async def test_reply_continues_with_full_history(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    seen: list[int] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(len(messages))
        return ModelResponse(parts=[TextPart(f"reply to {last_user_prompt(messages)}")])

    models.models["m"] = scripted(fn)
    session = await start(container, alpha, "one")
    await drain(container)
    await reply(container, session.id, "two")
    await drain(container)

    assert seen == [1, 3]
    history = await container.sessions.history(session.id)
    assert [type(m).__name__ for m in history] == ["ModelRequest", "ModelResponse"] * 2


async def test_reflection_runs_every_n_turns_over_the_window_and_can_write_skills(
    container, make_agent, models
):
    alpha = (await make_agent("alpha", model="m", reflect_every=2)).id
    transcripts: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = last_user_prompt(messages)
        if "scheduled self-review" not in prompt:
            return ModelResponse(parts=[TextPart("done")])
        if tool_returns(messages):
            return ModelResponse(parts=[TextPart("Created skill tea-making.")])
        transcripts.append(prompt)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "write_skill", {"name": "tea-making", "description": "Make tea", "content": "boil"}
                )
            ]
        )

    models.models["m"] = scripted(fn)
    await start(container, alpha, "first task", origin=Origin.TRIGGER)
    await start(container, alpha, "second task", origin=Origin.TRIGGER)
    await drain(container)

    reflections = await tasks_of(container, TaskKind.REFLECTION)
    assert len(reflections) == 1 and reflections[0].status == TaskStatus.DONE
    assert "first task" in transcripts[0] and "second task" in transcripts[0]
    assert container.skills.read(alpha, "tea-making").meta.description == "Make tea"
    async with container.db.transaction() as s:
        rows = (await s.scalars(select(Reflection))).all()
    assert [r.output for r in rows] == ["Created skill tea-making."]
    agent = await container.agents.get(alpha)
    assert agent.turns_since_reflection == 0
    assert agent.reflection_checkpoint == reflections[0].payload["to_message_id"]


async def test_transient_model_error_is_retried_and_converges(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    calls = {"n": 0}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelHTTPError(status_code=503, model_name="m", body="overloaded")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    session = await start(container, alpha, "hi", origin=Origin.TRIGGER)
    await drain(container)

    turn = (await tasks_of(container, TaskKind.TURN))[0]
    assert turn.status == TaskStatus.DONE and turn.attempts == 2
    assert len(await container.sessions.history(session.id)) == 2


async def test_contract_violation_error_fails_without_retry(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="m", body="bad request")

    models.models["m"] = scripted(fn)
    await start(container, alpha, "hi")
    await drain(container)

    turn = (await tasks_of(container, TaskKind.TURN))[0]
    assert turn.status == TaskStatus.FAILED and turn.attempts == 1
    assert "400" in turn.error


async def test_delegate_task_opens_public_thread_and_notifies_user(container, make_agent, models):
    router = (await make_agent("router", model="r")).id
    worker = (await make_agent("worker", model="w")).id
    models.models["r"] = call_then_answer(
        "delegate_task", {"agent": "worker", "task": "summarise X", "thread": "x"}, "delegated"
    )
    models.models["w"] = scripted(lambda m, i: ModelResponse(parts=[TextPart("summary of X")]))

    parent = await start(container, router, "new slack messages", origin=Origin.TRIGGER)
    await drain(container)

    children = await container.sessions.list(agent_id=worker, origins=None, limit=10, offset=0)
    assert len(children) == 1
    child = children[0]
    assert child.origin == Origin.DELEGATION and child.parent_session_id == parent.id
    assert child.thread == "x" and not child.private
    assert (await container.sessions.history(child.id))[-1].parts[0].content == "summary of X"
    deliver = await tasks_of(container, TaskKind.DELIVER)
    assert [d.session_id for d in deliver] == [child.id]


async def test_domain_error_in_tool_is_returned_to_model_for_correction(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    retry_texts: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        from pydantic_ai.messages import RetryPromptPart

        retries = (
            [p for p in last.parts if isinstance(p, RetryPromptPart)]
            if isinstance(last, ModelRequest)
            else []
        )
        if retries:
            retry_texts.append(str(retries[0].content))
            return ModelResponse(parts=[TextPart("sorry")])
        return ModelResponse(parts=[ToolCallPart("read_skill", {"name": "missing"})])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "use skill", origin=Origin.TRIGGER)
    await drain(container)

    assert "not found" in retry_texts[0]
    assert (await tasks_of(container, TaskKind.TURN))[0].status == TaskStatus.DONE


async def test_run_events_are_published_to_bus(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    models.models["m"] = call_then_answer("list_agents", {}, "there is one agent")
    events = []

    async def collect():
        async with container.bus.subscribe() as sub:
            async for e in sub:
                events.append(e)

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await start(container, alpha, "who is there", origin=Origin.TRIGGER)
    await drain(container)
    collector.cancel()

    kinds = [(e["type"], e.get("event") or e.get("status")) for e in events]
    assert ("run", "tool_call") in kinds and ("run", "tool_result") in kinds
    assert ("task", "done") in kinds


async def test_model_sees_agent_names_never_ids(container, make_agent, models):
    """原则：给模型看的是名称和描述，给程序用的是 id。"""
    router = await make_agent("router", model="r", description="分派任务")
    worker = await make_agent("研究助理", model="w", description="查资料")
    seen: list[str] = []
    steps = [
        ("list_agents", {}),
        ("delegate_task", {"agent": "研究助理", "task": "调研 X", "thread": "调研"}),
        ("delegate_task", {"agent": "不存在的助理", "task": "x", "thread": "x"}),
        (
            "trigger_create",
            {
                "name": "daily",
                "kind": "schedule",
                "prompt": "汇总",
                "config": {"cron": "0 9 * * *"},
                "agent": "研究助理",
            },
        ),
        ("trigger_list", {}),
    ]

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        seen.append(repr(messages))
        step = sum(isinstance(m, ModelResponse) for m in messages)
        if step < len(steps):
            name, args = steps[step]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    models.models["r"] = scripted(fn)
    await start(container, router.id, "安排工作", origin=Origin.TRIGGER)
    await drain(container)

    transcript = "\n".join(seen)
    assert "研究助理" in transcript and "查资料" in transcript
    assert (
        "no agent named '不存在的助理'" in transcript and "'研究助理'" in transcript
    )  # 写错名称时给出可选名称
    # 工作目录等文件路径里会出现 id（agents/<id>/…），那是给文件操作用的路径；除此之外模型不该看到 id
    for agent_id in (router.id, worker.id):
        assert agent_id not in re.sub(r"(workspaces|agents)/[0-9a-f]{12}", r"\1/<dir>", transcript)
    jobs = await container.triggers.list()
    assert [(j.name, j.agent_id) for j in jobs] == [("daily", worker.id)]  # 程序侧落到 id
    children = await container.sessions.list(agent_id=worker.id, origins=None, limit=10, offset=0)
    assert len(children) == 1
