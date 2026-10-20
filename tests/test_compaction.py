from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from muliao.models import Compaction, Message, Origin, Task, TaskKind, TaskStatus
from muliao.runtime.compaction import COMPACTION_INSTRUCTIONS, CompactionScheduler, select_turns
from muliao.runtime.tasks import session_lane
from tests.scripted import scripted
from tests.test_flow import drain, last_user_prompt, reply, start


class Recorder:
    """记录每次模型调用：对话轮次回复 "ok"，压缩请求回复固定摘要。"""

    def __init__(self) -> None:
        self.turns: list[tuple[str, int, str | None]] = []  # (prompt, 消息数, instructions)
        self.compactions: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = last_user_prompt(messages)
        if info.instructions == COMPACTION_INSTRUCTIONS:
            self.compactions.append(prompt)
            return ModelResponse(parts=[TextPart(f"SUMMARY#{len(self.compactions)}")])
        self.turns.append((prompt, len(messages), info.instructions))
        return ModelResponse(parts=[TextPart(f"re: {prompt}")])


async def _enqueue_compaction(container, session) -> None:
    async with container.db.transaction() as s:
        container.queue.enqueue(
            s,
            kind=TaskKind.COMPACTION,
            lane=session_lane(session.id),
            agent_id=session.agent_id,
            session_id=session.id,
            payload={},
        )


async def _session_with_turns(container, models, agent_id: str, n: int):
    recorder = Recorder()
    models.models["m"] = scripted(recorder)
    session = await start(container, agent_id, "turn 1", origin=Origin.TRIGGER)
    await drain(container)
    for i in range(2, n + 1):
        await reply(container, session.id, f"turn {i}")
        await drain(container)
    return session, recorder


async def test_compaction_summarizes_older_turns_and_next_turn_uses_summary(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m", reflect_every=100)).id
    keep = container.settings.runtime.compaction_keep_turns
    session, recorder = await _session_with_turns(container, models, alpha, keep + 2)

    await _enqueue_compaction(container, session)
    await drain(container)

    # 早于保留窗口的两轮被总结，保留窗口内的不在其中
    assert len(recorder.compactions) == 1
    assert "turn 1" in recorder.compactions[0] and "turn 2" in recorder.compactions[0]
    assert f"turn {keep + 2}" not in recorder.compactions[0]
    async with container.db.transaction() as s:
        compaction = await s.scalar(select(Compaction))
        turn2_last = await s.scalar(
            select(Message.id).where(Message.session_id == session.id).order_by(Message.id).offset(3).limit(1)
        )
    assert compaction.through_message_id == turn2_last  # 切点在第 2 轮末尾

    await reply(container, session.id, "after compaction")
    await drain(container)
    prompt, message_count, instructions = recorder.turns[-1]
    assert prompt == "after compaction"
    assert message_count == keep * 2 + 1  # 只有保留的轮次 + 本轮提问
    assert "<summary>\nSUMMARY#1\n</summary>" in instructions

    # UI 仍能看到完整原文
    assert len(await container.sessions.history(session.id)) == (keep + 3) * 2


async def test_second_compaction_folds_in_previous_summary(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m", reflect_every=100)).id
    keep = container.settings.runtime.compaction_keep_turns
    session, recorder = await _session_with_turns(container, models, alpha, keep + 1)
    await _enqueue_compaction(container, session)
    await drain(container)
    for i in range(2):
        await reply(container, session.id, f"more {i}")
        await drain(container)

    await _enqueue_compaction(container, session)
    await drain(container)

    assert len(recorder.compactions) == 2
    assert "<previous_summary>\nSUMMARY#1\n</previous_summary>" in recorder.compactions[1]
    assert "turn 1\n" not in recorder.compactions[1]  # 已压缩的原文不再重复发送


async def test_compaction_with_nothing_beyond_kept_turns_is_a_noop(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m", reflect_every=100)).id
    session, recorder = await _session_with_turns(container, models, alpha, 2)

    await _enqueue_compaction(container, session)
    await drain(container)

    assert recorder.compactions == []
    async with container.db.transaction() as s:
        assert await s.scalar(select(Compaction)) is None
        task = await s.scalar(select(Task).where(Task.kind == TaskKind.COMPACTION))
    assert task.status == TaskStatus.DONE


def _response(tokens: int) -> list[ModelMessage]:
    return [ModelResponse(parts=[TextPart("x")], usage=RequestUsage(input_tokens=tokens))]


async def _compaction_tasks(container) -> list[Task]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Task).where(Task.kind == TaskKind.COMPACTION))).all())


async def test_scheduler_enqueues_above_threshold_once_per_session(container, make_agent):
    alpha = (await make_agent("alpha")).id
    session = await start(container, alpha, "hi", origin=Origin.TRIGGER)
    scheduler = CompactionScheduler(container.queue, threshold_tokens=1000)

    async with container.db.transaction() as s:
        await scheduler.after_run(s, session, "m", _response(1000))
        await scheduler.after_run(s, session, "m", _response(1001))
    async with container.db.transaction() as s:
        await scheduler.after_run(s, session, "m", _response(5000))  # 已有待执行的压缩，不重复

    assert [t.lane for t in await _compaction_tasks(container)] == [session_lane(session.id)]


async def test_zero_usage_warns_instead_of_silently_never_compacting(container, make_agent, caplog):
    alpha = (await make_agent("alpha")).id
    session = await start(container, alpha, "hi", origin=Origin.TRIGGER)
    scheduler = CompactionScheduler(container.queue, threshold_tokens=1000)

    async with container.db.transaction() as s:
        await scheduler.after_run(s, session, "m", _response(0))

    assert "no input token usage" in caplog.text
    assert await _compaction_tasks(container) == []


def test_select_turns_bounds_batch_without_truncating():
    def turn(task_id: int, chars: int) -> list[Message]:
        payload = ModelResponse(parts=[TextPart("x" * chars)])
        from muliao.runtime.codec import dump_message

        return [Message(id=task_id, session_id="s", task_id=task_id, payload=dump_message(payload))]

    turns = [turn(1, 100), turn(2, 100), turn(3, 100), turn(4, 10)]
    chosen, more = select_turns(turns, keep_turns=1, max_chars=250)
    assert [t[0].task_id for t in chosen] == [1, 2] and more

    chosen, more = select_turns(turns, keep_turns=1, max_chars=10)  # 单轮超限也至少取一轮
    assert [t[0].task_id for t in chosen] == [1] and more

    chosen, more = select_turns(turns, keep_turns=1, max_chars=10_000)
    assert len(chosen) == 3 and not more


async def test_oversized_backlog_is_compacted_in_batches_until_converged(
    container, make_agent, models, monkeypatch
):
    import muliao.runtime.compaction as compaction

    monkeypatch.setattr(compaction, "_TRANSCRIPT_CHARS", 1)  # 每批只取一轮
    alpha = (await make_agent("alpha", model="m", reflect_every=100)).id
    keep = container.settings.runtime.compaction_keep_turns
    session, recorder = await _session_with_turns(container, models, alpha, keep + 3)

    await _enqueue_compaction(container, session)
    await drain(container)

    assert len(recorder.compactions) == 3  # 三轮可压缩内容分三批，自动续排
    async with container.db.transaction() as s:
        compactions = (await s.scalars(select(Compaction).order_by(Compaction.id))).all()
    assert [c.summary for c in compactions] == ["SUMMARY#1", "SUMMARY#2", "SUMMARY#3"]


async def test_manual_compaction_endpoint(container, make_agent, tmp_path):
    import httpx

    from muliao.api.app import create_app
    from tests.conftest import login

    alpha = (await make_agent("alpha")).id
    session = await start(container, alpha, "hi", origin=Origin.TRIGGER)
    app = create_app(container, tmp_path / "no-web")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        await login(client)
        first = await client.post(f"/api/sessions/{session.id}/compact")
        second = await client.post(f"/api/sessions/{session.id}/compact")
        missing = await client.post("/api/sessions/nope/compact")

    assert (first.status_code, first.json()) == (202, {"queued": True})
    assert second.json() == {"queued": False}
    assert missing.status_code == 404
