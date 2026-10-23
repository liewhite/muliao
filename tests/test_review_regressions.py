"""代码审查发现的问题的回归测试。"""

import httpx
import pytest
from anthropic import APIConnectionError
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import select

from muliao.agent_runtime.transient import is_transient, transient_as_retryable
from muliao.errors import Conflict, ModelUnavailable, TransientError
from muliao.notifications.service import DELIVER_TASK
from muliao.platform.tables import TaskStatus
from muliao.sessions.api import Origin
from muliao.sessions.lanes import RUN_TASK
from muliao.sessions.tables import Message
from muliao.sessions.transcript import to_items
from tests.test_flow import call_then_answer, drain, history_of, start


async def test_message_ids_never_reused_after_deleting_latest_session(container, make_agent):
    alpha = (await make_agent("alpha")).id
    first = await start(container, alpha, "one", origin=Origin.TRIGGER)
    await drain(container)
    async with container.db.transaction() as s:
        max_before = max((await s.scalars(select(Message.id))).all())
    await container.sessions.delete(first.id)

    await start(container, alpha, "two", origin=Origin.TRIGGER)
    await drain(container)

    async with container.db.transaction() as s:
        ids = (await s.scalars(select(Message.id))).all()
    assert min(ids) > max_before


async def test_next_due_ignores_tasks_blocked_by_running_lane(container, make_agent):
    alpha = (await make_agent()).id
    async with container.db.transaction() as s:
        for _ in range(2):
            container.queue.enqueue(
                s, kind=DELIVER_TASK, lane="x", agent_id=alpha, session_id=None, payload={}
            )
    await container.queue.claim(container.queue.kinds)

    # 第二个 task 被同 lane 的 running 挡住，不应让 worker 以为马上有活可干
    assert await container.queue.next_due(container.queue.kinds) is None


async def test_deleting_session_or_agent_with_running_task_is_rejected(container, make_agent):
    alpha = (await make_agent()).id
    session = await start(container, alpha, "hi")
    await container.queue.claim(container.queue.kinds)

    with pytest.raises(Conflict):
        await container.sessions.delete(session.id)
    with pytest.raises(Conflict):
        await container.agent_lifecycle.delete(alpha, author="user")


async def test_agent_api_rejects_partial_update(container, tmp_path):
    from tests.conftest import app_of, login

    app = app_of(container, tmp_path / "no-web")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        await login(client)
        agent = (
            await client.post("/api/agents", json={"name": "a", "description": "d", "model": "custom"})
        ).json()
        assert (
            await client.put(f"/api/agents/{agent['id']}", json={"description": "new"})
        ).status_code == 422
        assert (await client.get(f"/api/agents/{agent['id']}")).json()["model"] == "custom"


async def test_vault_set_value_is_redacted_from_saved_history(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    models.models["m"] = call_then_answer(
        "vault_set", {"key": "TOKEN", "value": "super-secret", "description": "d"}, "stored"
    )
    session = await start(container, alpha, "save my token", origin=Origin.TRIGGER)
    await drain(container)

    assert (await container.vault.resolve_many(alpha, ["TOKEN"])) == {"TOKEN": "super-secret"}
    async with container.db.transaction() as s:
        payloads = (await s.scalars(select(Message.payload).where(Message.session_id == session.id))).all()
    assert all("super-secret" not in p for p in payloads)
    calls = [i for i in to_items(await history_of(container, session.id)) if i.type == "tool_call"]
    assert "[redacted]" in calls[0].text


def test_only_transport_level_model_api_errors_are_transient():
    request = httpx.Request("POST", "https://api.example")
    connection = ModelAPIError(model_name="m", message="conn")
    connection.__cause__ = APIConnectionError(request=request)
    decode = ModelAPIError(model_name="m", message="bad json")
    decode.__cause__ = ValueError("x")

    assert is_transient(connection)
    assert not is_transient(decode)
    assert is_transient(ModelHTTPError(status_code=529, model_name="m"))
    assert not is_transient(ModelHTTPError(status_code=400, model_name="m"))
    assert is_transient(TransientError("x"))


async def test_the_worker_retries_only_transient_errors_and_timeouts_not_sdk_exceptions(container):
    # 平台不认识 SDK 的异常：暂时性的由 agent_runtime 换成 ModelUnavailable 之后才会重试
    retries = container.queue.retries
    assert retries.for_failure(TransientError("x")) is retries.transient
    assert retries.for_failure(TimeoutError()) is retries.transient
    assert retries.for_failure(ModelUnavailable("503")) is retries.model
    assert retries.for_failure(ModelHTTPError(status_code=503, model_name="m")) is None
    assert retries.for_failure(httpx.ConnectError("refused")) is None


def test_a_model_run_turns_transient_failures_into_transient_error_and_keeps_the_rest():
    request = httpx.Request("POST", "https://api.example")
    connection = ModelAPIError(model_name="m", message="conn")
    connection.__cause__ = APIConnectionError(request=request)
    with pytest.raises(ModelUnavailable, match="ModelAPIError") as caught:
        with transient_as_retryable():
            raise connection
    assert caught.value.__cause__ is connection

    # 工具、MCP 等适配层自己抛的 TransientError 不升级成模型不可用，仍按普通暂时性失败重试
    with pytest.raises(TransientError) as caught:
        with transient_as_retryable():
            raise TransientError("mcp down")
    assert type(caught.value) is TransientError

    with pytest.raises(ModelHTTPError):
        with transient_as_retryable():
            raise ModelHTTPError(status_code=400, model_name="m")


async def test_retried_task_keeps_last_error_while_running(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id

    def fail(_m, _i) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m")

    from tests.scripted import scripted

    models.models["m"] = scripted(fail)
    await start(container, alpha, "hi", origin=Origin.TRIGGER)
    await drain(container)
    turn = (await container.queue.list(status=TaskStatus.FAILED, limit=5))[0]
    assert turn.attempts == container.settings.runtime.model_retry_max_attempts and "503" in turn.error

    models.models["m"] = scripted(lambda _m, _i: ModelResponse(parts=[TextPart("ok")]))
    await container.queue.retry(turn.id)
    claimed = await container.queue.claim(container.queue.kinds)
    assert claimed.error is not None  # 运行期间仍能看到上次为什么失败


def test_vapid_private_key_is_owner_only(tmp_path):
    from muliao.notifications.webpush import VapidKeys

    path = tmp_path / "vapid.pem"
    VapidKeys(path)
    assert path.stat().st_mode & 0o777 == 0o600

    path.chmod(0o644)  # 旧版本生成的宽权限文件
    VapidKeys(path)
    assert path.stat().st_mode & 0o777 == 0o600


async def test_subscriber_survives_heartbeat_timeouts():
    import asyncio

    from muliao.platform.bus import EventBus

    bus = EventBus()
    async with bus.subscribe() as sub:
        for _ in range(3):  # SSE 每次心跳都是一次超时
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(sub.next(), timeout=0.01)
        from muliao.sessions.live import SavedEvent

        bus.publish(SavedEvent(session_id="s"))
        assert await asyncio.wait_for(sub.next(), timeout=1) == SavedEvent(session_id="s")


def test_unparseable_secret_tool_arguments_are_redacted_wholesale():
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from muliao.agent_runtime.redaction import redact_messages

    cut = ModelResponse(
        parts=[ToolCallPart("vault_set", '{"key": "T", "value": "super-sec', tool_call_id="v")]
    )
    assert "super-sec" not in str(redact_messages([cut])[0].parts[0].args)


async def test_retrying_a_failed_run_after_the_session_was_stopped_ends_quietly(
    container, make_agent, models
):
    """最终失败的运行，会话被停止之后再手动重试：历史停在标着停止的请求上，没有要交给模型的，按「没有要交给模型的」
    收尾，不当成「已跑完只差收尾」（那要求历史停在一条回复上）。"""
    from muliao.agent_runtime.api import is_stopped
    from tests.scripted import scripted
    from tests.test_flow import tasks_of

    alpha = (await make_agent("alpha", model="m")).id
    calls = {"n": 0}

    def fail(_m, _i) -> ModelResponse:
        calls["n"] += 1
        raise ModelHTTPError(status_code=400, model_name="m", body="bad request")

    models.models["m"] = scripted(fail)
    session = await start(container, alpha, "hi", origin=Origin.TRIGGER)
    await drain(container)
    [failed] = await tasks_of(container, RUN_TASK)
    assert failed.status == TaskStatus.FAILED

    await container.stopper(session.id)
    await container.queue.retry(failed.id)
    await drain(container)

    [run] = await tasks_of(container, RUN_TASK)
    assert run.status == TaskStatus.DONE
    assert calls["n"] == 1
    history = await history_of(container, session.id)
    assert is_stopped(history[-1])


async def test_mail_taken_at_a_runs_start_counts_as_handed_to_it(container, make_agent, models):
    """运行开头取走的信件比投递它时记下的多：记进它的 payload，最终失败时据此了结、不为它们再唤醒一次。"""
    from muliao.sessions.mailbox import RunPayload
    from tests.scripted import scripted
    from tests.test_flow import mail_of, reply, tasks_of

    alpha = (await make_agent("alpha", model="m")).id
    models.models["m"] = scripted(lambda _m, _i: ModelResponse(parts=[TextPart("ok")]))
    session = await start(container, alpha, "one", origin=Origin.TRIGGER)
    # 运行还在排队：这封信不另外唤醒，由它开头一起取走
    later = await reply(container, session.id, "two")
    [run] = await tasks_of(container, RUN_TASK)
    assert RunPayload.of(run).through_mail_id < later.id

    await drain(container)

    [run] = await tasks_of(container, RUN_TASK)
    assert RunPayload.of(run).through_mail_id == later.id
    assert {m.consumed_by for m in await mail_of(container)} == {run.id}


async def test_a_vault_set_call_failing_validation_keeps_the_value_out_of_history_and_live_events(
    container, make_agent, models, monkeypatch
):
    from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolCallPart

    alpha = (await make_agent("alpha", model="m")).id

    def fn(messages, info):
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(isinstance(p, RetryPromptPart) for p in last.parts):
            return ModelResponse(parts=[TextPart("gave up")])
        # 缺必填的 key：校验失败的错误里带着整个参数 dict
        return ModelResponse(parts=[ToolCallPart("vault_set", {"value": "super-secret"})])

    from tests.test_flow import scripted

    models.models["m"] = scripted(fn)
    published = []
    real = container.live.publish_item
    monkeypatch.setattr(container.live, "publish_item", lambda *a: (published.append(a[2]), real(*a))[1])
    session = await start(container, alpha, "save my token", origin=Origin.TRIGGER)
    await drain(container)

    async with container.db.transaction() as s:
        payloads = (await s.scalars(select(Message.payload).where(Message.session_id == session.id))).all()
    assert any("RetryPromptPart" in p or "retry-prompt" in p for p in payloads)
    assert all("super-secret" not in p for p in payloads)
    assert published and all("super-secret" not in item.model_dump_json() for item in published)


async def test_running_out_of_vault_set_retries_keeps_the_value_out_of_the_logged_failure(
    container, make_agent, models, caplog
):
    import logging

    from pydantic_ai.messages import ToolCallPart

    from tests.test_flow import scripted

    caplog.set_level(logging.INFO)
    alpha = (await make_agent("alpha", model="m")).id
    # 每次都缺必填的 key：重试次数用完后运行失败，失败连同 traceback 进日志
    models.models["m"] = scripted(
        lambda messages, info: ModelResponse(parts=[ToolCallPart("vault_set", {"value": "super-secret"})])
    )
    session = await start(container, alpha, "save my token", origin=Origin.TRIGGER)
    await drain(container)

    assert "exceeded max retries" in caplog.text
    assert "super-secret" not in caplog.text
    async with container.db.transaction() as s:
        payloads = (await s.scalars(select(Message.payload).where(Message.session_id == session.id))).all()
    assert all("super-secret" not in p for p in payloads)


async def test_every_model_run_entry_hands_transient_failures_to_the_queue_as_model_unavailable(
    container, make_agent, models, tmp_path
):
    """worker 不认识 SDK 的异常：压缩（summarize）与临时 agent（run_temp）这些入口同样要换成 ModelUnavailable 才会按模型策略重试。"""
    from muliao.agent_runtime.runner import RunProfile

    agent = await make_agent("alpha", model="m")

    def overloaded(_m, _i) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m")

    from tests.scripted import scripted

    models.models["m"] = scripted(overloaded)
    runtime = container.consumer._runtime  # noqa: SLF001
    with pytest.raises(ModelUnavailable, match="503"):
        await runtime.summarize(agent, "s1", "summarize", "text")
    with pytest.raises(ModelUnavailable, match="503"):
        await runtime.run_temp(
            agent,
            task="t",
            instructions="",
            model="m",
            thinking="default",
            profile=RunProfile(toolsets=[]),
            workspace=tmp_path,
            session_id=None,
        )
