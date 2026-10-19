from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from muliao.models import Compaction, DeliveryChannel, Message, Task, TaskKind, TaskStatus
from muliao.runtime.compaction import COMPACTION_INSTRUCTIONS, CompactionTrigger
from muliao.runtime.tasks import turn_lane
from tests.scripted import scripted
from tests.test_flow import drain, last_user_prompt, start


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
            lane=turn_lane(session.id),
            agent_name=session.agent_name,
            session_id=session.id,
            payload={},
        )


async def _session_with_turns(container, models, n: int):
    recorder = Recorder()
    models.models["m"] = scripted(recorder)
    session = await start(container, "alpha", "turn 1", delivery=DeliveryChannel.NONE)
    await drain(container)
    for i in range(2, n + 1):
        await container.sessions.reply(session.id, f"turn {i}")
        await drain(container)
    return session, recorder


async def test_compaction_summarizes_older_turns_and_next_turn_uses_summary(container, make_agent, models):
    await make_agent("alpha", model="m", reflect_every=100)
    keep = container.settings.runtime.compaction_keep_turns
    session, recorder = await _session_with_turns(container, models, keep + 2)

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

    await container.sessions.reply(session.id, "after compaction")
    await drain(container)
    prompt, message_count, instructions = recorder.turns[-1]
    assert prompt == "after compaction"
    assert message_count == keep * 2 + 1  # 只有保留的轮次 + 本轮提问
    assert "<summary>\nSUMMARY#1\n</summary>" in instructions

    # UI 仍能看到完整原文
    assert len(await container.sessions.history(session.id)) == (keep + 3) * 2


async def test_second_compaction_folds_in_previous_summary(container, make_agent, models):
    await make_agent("alpha", model="m", reflect_every=100)
    keep = container.settings.runtime.compaction_keep_turns
    session, recorder = await _session_with_turns(container, models, keep + 1)
    await _enqueue_compaction(container, session)
    await drain(container)
    for i in range(2):
        await container.sessions.reply(session.id, f"more {i}")
        await drain(container)

    await _enqueue_compaction(container, session)
    await drain(container)

    assert len(recorder.compactions) == 2
    assert "<previous_summary>\nSUMMARY#1\n</previous_summary>" in recorder.compactions[1]
    assert "turn 1\n" not in recorder.compactions[1]  # 已压缩的原文不再重复发送


async def test_compaction_with_nothing_beyond_kept_turns_is_a_noop(container, make_agent, models):
    await make_agent("alpha", model="m", reflect_every=100)
    session, recorder = await _session_with_turns(container, models, 2)

    await _enqueue_compaction(container, session)
    await drain(container)

    assert recorder.compactions == []
    async with container.db.transaction() as s:
        assert await s.scalar(select(Compaction)) is None
        task = await s.scalar(select(Task).where(Task.kind == TaskKind.COMPACTION))
    assert task.status == TaskStatus.DONE


async def test_trigger_enqueues_only_above_threshold(container, make_agent):
    await make_agent("alpha")
    session = await start(container, "alpha", "hi", delivery=DeliveryChannel.NONE)
    trigger = CompactionTrigger(container.queue, threshold_tokens=1000)

    def response(tokens: int) -> list[ModelMessage]:
        return [ModelResponse(parts=[TextPart("x")], usage=RequestUsage(input_tokens=tokens))]

    async with container.db.transaction() as s:
        trigger.maybe_enqueue(s, session, response(1000))
        trigger.maybe_enqueue(s, session, response(1001))
    async with container.db.transaction() as s:
        tasks = (await s.scalars(select(Task).where(Task.kind == TaskKind.COMPACTION))).all()
    assert [t.lane for t in tasks] == [turn_lane(session.id)]
