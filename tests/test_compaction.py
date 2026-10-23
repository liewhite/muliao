"""上下文压缩：步与步之间切分，手动压缩与运行中自动压缩共用 Compactor.compact；超过上限分批压缩直到收敛。"""

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from muliao.agent_runtime.conventions import run_context_index
from muliao.agents.api import AgentPatch
from muliao.platform.tables import Task, TaskStatus
from muliao.sessions.api import Origin
from muliao.sessions.codec import dump_message, load_messages
from muliao.sessions.compaction import COMPACTION_INSTRUCTIONS, select_steps, steps_of
from muliao.sessions.history import CONTINUATION
from muliao.sessions.lanes import COMPACTION_TASK, RUN_TASK, session_lane
from muliao.sessions.tables import Compaction, Message
from tests.scripted import scripted
from tests.test_flow import drain, history_of, last_user_prompt, reply, start


class Recorder:
    """记录每次模型调用：对话回复 "re: ..."，压缩请求回复固定摘要。"""

    def __init__(self) -> None:
        self.turns: list[tuple[str, int, str | None]] = []  # (prompt, 消息数, instructions)
        self.compactions: list[str] = []
        self.compaction_thinking: list[object] = []  # 压缩请求带上的思考档位

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = last_user_prompt(messages)
        if info.instructions == COMPACTION_INSTRUCTIONS:
            self.compactions.append(prompt)
            self.compaction_thinking.append(info.model_request_parameters.thinking)
            return ModelResponse(parts=[TextPart(f"SUMMARY#{len(self.compactions)}")])
        self.turns.append((prompt, len(messages), info.instructions))
        return ModelResponse(parts=[TextPart(f"re: {prompt}")])


async def _enqueue_compaction(container, session) -> None:
    async with container.db.transaction() as s:
        container.queue.enqueue(
            s,
            kind=COMPACTION_TASK,
            lane=session_lane(session.id),
            agent_id=session.agent_id,
            session_id=session.id,
            payload={},
        )


async def _session_with_exchanges(
    container, models, agent_id: str, n: int, profile: ModelProfile | None = None
):
    """n 次一问一答（没有工具调用：每条消息各是一步）。"""
    recorder = Recorder()
    models.models["m"] = scripted(recorder, profile)
    session = await start(container, agent_id, "turn 1", origin=Origin.TRIGGER)
    await drain(container)
    for i in range(2, n + 1):
        await reply(container, session.id, f"turn {i}")
        await drain(container)
    return session, recorder


def _row(i: int, message: ModelMessage) -> Message:
    return Message(id=i, session_id="s", task_id=1, payload=dump_message(message))


def test_steps_split_where_no_call_is_left_dangling():
    call = ModelResponse(parts=[ToolCallPart("memory_append", {"entry": "x"}, tool_call_id="c")])
    result = ModelRequest(parts=[ToolReturnPart("memory_append", "ok", tool_call_id="c")])
    rows = [
        _row(1, ModelRequest(parts=[UserPromptPart("go")])),
        _row(2, call),
        _row(3, result),
        _row(4, ModelResponse(parts=[TextPart("done")])),
        _row(5, ModelRequest(parts=[UserPromptPart("next")])),
    ]
    # 工具调用与它的结果在同一步；运行中途（结果之后、下一次回复之前）也是切点
    assert [[r.id for r in step] for step in steps_of(rows)] == [[1], [2, 3], [4], [5]]


async def test_compaction_summarizes_older_steps_and_the_next_run_uses_the_summary(
    container, make_agent, models
):
    alpha = (await make_agent("alpha", model="m")).id
    keep = container.settings.runtime.compaction_keep_steps
    n = keep // 2 + 2
    session, recorder = await _session_with_exchanges(container, models, alpha, n)

    await _enqueue_compaction(container, session)
    await drain(container)

    # 保留窗口之外的四步（前两次问答）被总结
    assert len(recorder.compactions) == 1
    assert recorder.compaction_thinking == [None]  # 思考档位 default：什么都不传
    assert "turn 1" in recorder.compactions[0] and "turn 2" in recorder.compactions[0]
    assert f"turn {n}" not in recorder.compactions[0]
    async with container.db.transaction() as s:
        compaction = await s.scalar(select(Compaction))
        fourth = await s.scalar(
            select(Message.id).where(Message.session_id == session.id).order_by(Message.id).offset(3).limit(1)
        )
    assert compaction.through_message_id == fourth

    await reply(container, session.id, "after compaction")
    await drain(container)
    prompt, message_count, instructions = recorder.turns[-1]
    assert prompt == "after compaction"
    assert message_count == keep + 1  # 只有保留的步 + 这次的提问
    assert "<summary>\nSUMMARY#1\n</summary>" in instructions
    # UI 仍能看到完整原文
    assert len(await history_of(container, session.id)) == (n + 1) * 2


async def test_second_compaction_folds_in_previous_summary(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    keep = container.settings.runtime.compaction_keep_steps
    session, recorder = await _session_with_exchanges(container, models, alpha, keep // 2 + 1)
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


async def test_compaction_with_nothing_beyond_kept_steps_is_a_noop(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    session, recorder = await _session_with_exchanges(container, models, alpha, 2)

    await _enqueue_compaction(container, session)
    await drain(container)

    assert recorder.compactions == []
    async with container.db.transaction() as s:
        assert await s.scalar(select(Compaction)) is None
        task = await s.scalar(select(Task).where(Task.kind == COMPACTION_TASK))
    assert task.status == TaskStatus.DONE


def _response(tokens: int) -> list[ModelMessage]:
    return [ModelResponse(parts=[TextPart("x")], usage=RequestUsage(input_tokens=tokens))]


async def test_threshold_is_the_size_of_the_last_request(container):
    compactor = container.compactor
    threshold = container.settings.runtime.compaction_threshold_tokens
    assert not compactor.over_threshold("s", [])
    assert not compactor.over_threshold("s", _response(threshold))
    assert compactor.over_threshold("s", _response(threshold + 1))


async def test_zero_usage_warns_instead_of_silently_never_compacting(container, caplog):
    assert not container.compactor.over_threshold("s", _response(0))
    assert "no input token usage" in caplog.text


def test_select_steps_bounds_batch_without_truncating():
    def step(i: int, chars: int) -> list[Message]:
        return [_row(i, ModelResponse(parts=[TextPart("x" * chars)]))]

    steps = [step(1, 100), step(2, 100), step(3, 100), step(4, 10)]
    chosen, more = select_steps(steps, keep_steps=1, max_chars=250)
    assert [s[0].id for s in chosen] == [1, 2] and more

    chosen, more = select_steps(steps, keep_steps=1, max_chars=10)  # 单步超限也至少取一步
    assert [s[0].id for s in chosen] == [1] and more

    chosen, more = select_steps(steps, keep_steps=1, max_chars=10_000)
    assert len(chosen) == 3 and not more


async def test_oversized_backlog_is_compacted_in_batches_until_converged(
    container, make_agent, models, monkeypatch
):
    import muliao.sessions.compaction as compaction

    monkeypatch.setattr(compaction, "_TRANSCRIPT_CHARS", 1)  # 每批只取一步
    alpha = (await make_agent("alpha", model="m")).id
    keep = container.settings.runtime.compaction_keep_steps
    session, recorder = await _session_with_exchanges(container, models, alpha, keep // 2 + 3)

    await _enqueue_compaction(container, session)
    await drain(container)

    assert len(recorder.compactions) == 6  # 六步可压缩内容分六批
    async with container.db.transaction() as s:
        compactions = (await s.scalars(select(Compaction).order_by(Compaction.id))).all()
    assert [c.summary for c in compactions] == [f"SUMMARY#{i}" for i in range(1, 7)]


async def test_a_long_run_is_compacted_between_steps_and_resumes(container, make_agent, models, monkeypatch):
    """运行中途上下文超过阈值：这次请求保存后停下、压缩，再从它续跑；留下的部分从一条回复开始时补接续的用户消息。"""
    monkeypatch.setattr(container.compactor, "_threshold", 1)
    monkeypatch.setattr(container.compactor, "_keep_steps", 1)
    alpha = (await make_agent("alpha", model="m")).id
    seen: list[tuple[list[ModelMessage], str | None]] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.instructions == COMPACTION_INSTRUCTIONS:
            return ModelResponse(parts=[TextPart("SUMMARY")])
        seen.append((list(messages), info.instructions))
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("memory_append", {"entry": "- once"}, tool_call_id="c")])
        return ModelResponse(parts=[TextPart("done")])

    models.models["m"] = scripted(fn)
    session = await start(container, alpha, "go", origin=Origin.TRIGGER)
    await drain(container)

    assert len(seen) == 2
    resumed, instructions = seen[1]
    assert isinstance(resumed[0], ModelRequest) and resumed[0].parts[0].content == CONTINUATION
    assert "SUMMARY" in (instructions or "")
    # 工具只执行一次，运行只有一次
    assert container.memory.read(alpha) == "- once\n"
    runs = [t for t in await _tasks(container, RUN_TASK)]
    assert [r.status for r in runs] == [TaskStatus.DONE]
    history = await history_of(container, session.id)
    assert isinstance(history[-1], ModelResponse) and history[-1].parts[0].content == "done"


async def _tasks(container, kind: str) -> list[Task]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Task).where(Task.kind == kind))).all())


async def test_manual_compaction_endpoint(container, make_agent, tmp_path):
    import httpx

    from tests.conftest import app_of, login

    alpha = (await make_agent("alpha")).id
    session = await start(container, alpha, "hi", origin=Origin.TRIGGER)
    app = app_of(container, tmp_path / "no-web")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        await login(client)
        first = await client.post(f"/api/sessions/{session.id}/compact")
        second = await client.post(f"/api/sessions/{session.id}/compact")
        missing = await client.post("/api/sessions/nope/compact")

    assert (first.status_code, first.json()) == (202, {"queued": True})
    assert second.json() == {"queued": False}
    assert missing.status_code == 404


async def test_stopping_while_compacting_between_steps_leaves_one_result_per_call(
    container, make_agent, models, monkeypatch
):
    """停在步间压缩的途中：那次请求（带着工具结果）已经保存，停止不能再给这些调用补一份「被停止」的结果。"""
    import asyncio

    from muliao.agent_runtime.conventions import calls_in, results_in
    from muliao.agent_runtime.runner import AgentRuntime
    from tests.test_flow import idle

    monkeypatch.setattr(container.compactor, "_threshold", 1)
    monkeypatch.setattr(container.compactor, "_keep_steps", 1)
    entered = asyncio.Event()

    async def blocked(self, agent, session_id, instructions, prompt):  # noqa: ANN001, ANN202
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AgentRuntime, "summarize", blocked)
    alpha = (await make_agent("alpha", model="m")).id

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("memory_append", {"entry": "- x"}, tool_call_id="c")])

    models.models["m"] = scripted(fn)
    session = await start(container, alpha, "go", origin=Origin.TRIGGER)
    worker = asyncio.create_task(container.worker.run_forever())
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        await container.stopper(session.id)
        await idle(container)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    history = await history_of(container, session.id)
    results = [r for m in history for r in results_in(m)]
    assert sorted(results) == sorted(c.tool_call_id for m in history for c in calls_in(m))
    assert len(results) == len(set(results))


async def test_a_run_that_compacts_at_its_start_does_not_compact_again_before_a_new_reply(
    container, make_agent, models, monkeypatch
):
    alpha = (await make_agent("alpha", model="m")).id
    session, recorder = await _session_with_exchanges(container, models, alpha, 3)
    monkeypatch.setattr(container.compactor, "_threshold", 1)
    monkeypatch.setattr(container.compactor, "_keep_steps", 1)

    await reply(container, session.id, "go on")
    await drain(container)

    # 运行开始时压缩一次；第一次请求时上一条回复的 token 数还是压缩前的，不能据此再压
    assert len(recorder.compactions) == 1


async def test_after_a_manual_compaction_a_long_run_still_compacts_mid_way(
    container, make_agent, models, monkeypatch
):
    """手动压缩只写摘要、不产生新回复：之后那次运行开头不能拿压缩前的 token 数判断，更不能因此整次运行不再压缩。"""
    import muliao.sessions.compaction as compaction

    # 回复的上下文大小由脚本标在回复上（流式的 function model 自己估算 usage）：标了 big 的算超阈值
    def tokens(history):  # noqa: ANN001, ANN202
        last = next((m for m in reversed(history) if isinstance(m, ModelResponse)), None)
        return None if last is None else 5000 if "big" in str(last.parts) else 100

    monkeypatch.setattr(compaction, "context_tokens", tokens)
    monkeypatch.setattr(container.compactor, "_threshold", 1000)
    monkeypatch.setattr(container.compactor, "_keep_steps", 1)
    alpha = (await make_agent("alpha", model="m")).id
    summaries = {"n": 0}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.instructions == COMPACTION_INSTRUCTIONS:
            summaries["n"] += 1
            return ModelResponse(parts=[TextPart(f"SUMMARY#{summaries['n']}")])
        if last_user_prompt(messages) != "long task":
            return ModelResponse(parts=[TextPart("big answer")])
        # 长任务：第一步上下文小，第二步变大，第三步收尾
        done = sum(1 for m in messages if isinstance(m, ModelResponse) and "c-" in str(m.parts))
        if done == 0:
            return ModelResponse(
                parts=[ToolCallPart("memory_append", {"entry": "- a"}, tool_call_id="c-small")]
            )
        if done == 1:
            return ModelResponse(
                parts=[ToolCallPart("memory_append", {"entry": "- b"}, tool_call_id="c-big")]
            )
        return ModelResponse(parts=[TextPart("done")])

    models.models["m"] = scripted(fn)
    session = await start(container, alpha, "first", origin=Origin.TRIGGER)
    await drain(container)
    await reply(container, session.id, "second")
    await drain(container)
    await _enqueue_compaction(container, session)
    await drain(container)
    manual = summaries["n"]
    assert manual >= 1

    await reply(container, session.id, "long task")
    await drain(container)
    assert summaries["n"] > manual


async def test_a_run_compacted_past_its_own_start_gets_its_run_context_again(
    container, make_agent, models, monkeypatch
):
    """长运行中途压缩切过了它自己的第一条请求（挂着运行上下文的那条）：之后的新请求重新挂一段，模型仍看得到当前时间。"""
    monkeypatch.setattr(container.compactor, "_threshold", 1)
    monkeypatch.setattr(container.compactor, "_keep_steps", 1)
    alpha = (await make_agent("alpha", model="m")).id
    seen: list[list[ModelMessage]] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.instructions == COMPACTION_INSTRUCTIONS:
            return ModelResponse(parts=[TextPart("SUMMARY")])
        seen.append(list(messages))
        if len(seen) <= 2:
            entry = f"- step {len(seen)}"
            return ModelResponse(
                parts=[ToolCallPart("memory_append", {"entry": entry}, tool_call_id=f"c{len(seen)}")]
            )
        return ModelResponse(parts=[TextPart("done")])

    models.models["m"] = scripted(fn)
    session = await start(container, alpha, "go", origin=Origin.TRIGGER)
    await drain(container)

    assert len(seen) == 3
    async with container.db.transaction() as s:
        through = (await s.scalars(select(Compaction.through_message_id))).all()
    first_request = (await _rows(container, session.id))[0]
    assert run_context_index(first_request[1]) is not None and first_request[0] <= max(through)
    # 第一段已经压进摘要，模型最后一次看到的上下文里仍有一段运行上下文
    assert any(run_context_index(m) is not None for m in seen[-1])


async def _rows(container, session_id: str) -> list[tuple[int, ModelMessage]]:
    async with container.db.transaction() as s:
        rows = (
            await s.execute(
                select(Message.id, Message.payload)
                .where(Message.session_id == session_id)
                .order_by(Message.id)
            )
        ).all()
    return [(i, m) for (i, _), m in zip(rows, load_messages(p for _, p in rows), strict=True)]


async def test_compaction_uses_the_agents_thinking_level(container, make_agent, models):
    models.models["m"] = scripted(Recorder(), ModelProfile(supports_thinking=True))
    alpha = (await make_agent("alpha", model="m")).id
    await container.agents.update(alpha, AgentPatch(thinking="low"))
    n = container.settings.runtime.compaction_keep_steps // 2 + 2
    session, recorder = await _session_with_exchanges(
        container, models, alpha, n, ModelProfile(supports_thinking=True)
    )

    await _enqueue_compaction(container, session)
    await drain(container)

    assert recorder.compaction_thinking == ["low"]
