"""运行端口逐步交出新增消息（agent_runtime.StepHooks）：每条恰好交出一次，不论运行怎么结束；重发历史末尾那条请求时
不再交出它（docs/architecture.md「每一步保存」）。"""

import asyncio

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from muliao.agent_runtime.api import Finished, StepHooks
from muliao.platform.tooling import Audience
from tests.scripted import scripted


class _Recorder:
    """记下交出的消息（took 与 took_request）；每次 before_request 交来的请求必须已经是最后交出的那一条。"""

    def __init__(self) -> None:
        self.saved: list[ModelMessage] = []
        self.requests = 0

    def hooks(self) -> StepHooks:
        async def started(notices: object) -> None:
            return None

        def took_request(request: ModelRequest) -> None:
            self.saved.append(request)
            self.requests += 1

        async def before_request(request: ModelRequest) -> None:
            assert self.saved and self.saved[-1] is request

        async def after_response() -> None:
            return None

        return StepHooks(
            started=started,
            took=self.saved.extend,
            took_request=took_request,
            before_request=before_request,
            after_response=after_response,
            on_text=lambda kind, text: None,
            on_tool_call=lambda part: None,
            on_tool_result=lambda part: None,
        )


async def _drive(container, agent, prompt, history, recorder: _Recorder):  # noqa: ANN001, ANN202
    runtime = container.consumer._runtime  # noqa: SLF001
    config = await runtime.config(agent.id)
    profile = await container.consumer._toolbox.profile(agent)  # noqa: SLF001
    scope = runtime.scope(agent, session_id="s1", audience=Audience.USER)
    return await runtime.run_steps(
        config, scope, prompt, history, profile, recorder.hooks(), origin_note="note"
    )


async def test_every_new_message_is_handed_over_exactly_once(container, make_agent, models):
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            # 不存在的工具：pydantic-ai 在下一条请求里带回重试提示
            return ModelResponse(parts=[ToolCallPart("no_such_tool", {}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("done")])

    models.models["m"] = scripted(fn)
    agent = await make_agent("alpha", model="m")
    recorder = _Recorder()

    outcome = await _drive(container, agent, ["hi"], [], recorder)

    assert isinstance(outcome, Finished)
    assert recorder.saved == outcome.messages
    assert recorder.requests == 2


async def test_a_resent_request_is_not_handed_over_again(container, make_agent, models):
    models.models["m"] = scripted(lambda messages, info: ModelResponse(parts=[TextPart("ok")]))
    agent = await make_agent("alpha", model="m")
    recorder = _Recorder()
    saved_request = ModelRequest(parts=[UserPromptPart("already saved")])

    outcome = await _drive(container, agent, None, [saved_request], recorder)

    assert isinstance(outcome, Finished)
    # 重发的那条已经保存：不经 before_request、不经 took，只交出模型的回复
    assert recorder.requests == 0
    assert [type(m) for m in recorder.saved] == [ModelResponse]


async def test_a_run_never_resumes_from_a_provider_suspended_response(container, make_agent, models):
    """提供方暂停的回复（suspended）走 pydantic-ai 另一条续跑路线，请求不进历史：当场拒绝，不让交出下标错位。"""
    import pytest

    models.models["m"] = scripted(lambda messages, info: ModelResponse(parts=[TextPart("ok")]))
    agent = await make_agent("alpha", model="m")
    history = [ModelRequest(parts=[UserPromptPart("go")]), ModelResponse(parts=[], state="suspended")]

    with pytest.raises(RuntimeError, match="suspended"):
        await _drive(container, agent, None, history, _Recorder())


async def test_a_cancelled_run_hands_over_the_partial_answer(container, make_agent, models):
    hang = asyncio.Event()
    streaming = asyncio.Event()

    async def stream(messages: list[ModelMessage], info: AgentInfo):  # noqa: ANN202
        yield "half an"
        streaming.set()
        await hang.wait()

    models.models["m"] = FunctionModel(
        lambda _m, _i: ModelResponse(parts=[TextPart("unused")]), stream_function=stream
    )
    agent = await make_agent("alpha", model="m")
    recorder = _Recorder()

    run = asyncio.create_task(_drive(container, agent, ["hi"], [], recorder))
    await asyncio.wait_for(streaming.wait(), 10)
    await asyncio.sleep(0.05)
    run.cancel()
    try:
        await run
    except asyncio.CancelledError:
        pass

    [request, response] = recorder.saved
    assert isinstance(request, ModelRequest)
    assert isinstance(response, ModelResponse) and response.state == "interrupted"
    assert [p.content for p in response.parts if isinstance(p, TextPart)] == ["half an"]
