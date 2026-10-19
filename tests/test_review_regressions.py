"""代码审查发现的问题的回归测试。"""

from datetime import datetime, timedelta

import httpx
import pytest
from anthropic import APIConnectionError
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import select

from muliao.delivery.base import Notification
from muliao.delivery.webpush import WebPushDelivery
from muliao.errors import Conflict, TransientError
from muliao.models import CronJob, DeliveryChannel, Message, Task, TaskKind, TaskStatus
from muliao.runtime.worker import is_transient
from muliao.transcript import to_items
from tests.test_flow import call_then_answer, drain, start


async def test_message_ids_never_reused_after_deleting_latest_session(container, make_agent):
    await make_agent("alpha", reflect_every=100)
    first = await start(container, "alpha", "one", delivery=DeliveryChannel.NONE)
    await drain(container)
    async with container.db.transaction() as s:
        max_before = max((await s.scalars(select(Message.id))).all())
    await container.sessions.delete(first.id)

    await start(container, "alpha", "two", delivery=DeliveryChannel.NONE)
    await drain(container)

    async with container.db.transaction() as s:
        ids = (await s.scalars(select(Message.id))).all()
    assert min(ids) > max_before


async def test_next_due_ignores_tasks_blocked_by_running_lane(container, make_agent):
    await make_agent()
    async with container.db.transaction() as s:
        for _ in range(2):
            container.queue.enqueue(
                s, kind=TaskKind.DELIVER, lane="x", agent_name="alpha", session_id=None, payload={}
            )
    await container.queue.claim()

    # 第二个 task 被同 lane 的 running 挡住，不应让 worker 以为马上有活可干
    assert await container.queue.next_due() is None


async def test_deleting_session_or_agent_with_running_task_is_rejected(container, make_agent):
    await make_agent()
    session = await start(container, "alpha", "hi")
    await container.queue.claim()

    with pytest.raises(Conflict):
        await container.sessions.delete(session.id)
    with pytest.raises(Conflict):
        await container.agents.delete("alpha")


async def test_broken_cron_job_is_disabled_without_blocking_others(container, make_agent):
    await make_agent()
    bad = await container.cron.create(name="bad", agent_name="alpha", schedule="* * * * *", prompt="x")
    good = await container.cron.create(name="good", agent_name="alpha", schedule="* * * * *", prompt="y")
    async with container.db.transaction() as s:
        for job_id in (bad.id, good.id):
            (await s.get(CronJob, job_id)).next_run_at = datetime.now().astimezone() - timedelta(seconds=1)
        (await s.get(CronJob, bad.id)).prompt = "   "  # 模拟绕过校验写入的坏数据

    await container.cron.fire_due()

    bad, good = await container.cron.get_by_name("bad"), await container.cron.get_by_name("good")
    assert not bad.enabled and "prompt must not be empty" in bad.last_error
    assert good.last_session_id is not None


async def test_agent_api_rejects_zero_reflect_every_and_partial_update(container):
    from muliao.api.app import create_app
    from tests.test_api import login

    app = create_app(container, container.settings.web_dir)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        await login(client)
        resp = await client.post("/api/agents", json={"name": "a", "description": "d", "reflect_every": 0})
        assert resp.status_code == 422
        await client.post("/api/agents", json={"name": "a", "description": "d", "model": "custom"})
        assert (await client.put("/api/agents/a", json={"description": "new"})).status_code == 422
        assert (await client.get("/api/agents/a")).json()["model"] == "custom"


async def test_vault_set_value_is_redacted_from_saved_history(container, make_agent, models):
    await make_agent("alpha", model="m")
    models.models["m"] = call_then_answer(
        "vault_set", {"key": "TOKEN", "value": "super-secret", "description": "d"}, "stored"
    )
    session = await start(container, "alpha", "save my token", delivery=DeliveryChannel.NONE)
    await drain(container)

    assert (await container.vault.resolve_many(["TOKEN"])) == {"TOKEN": "super-secret"}
    async with container.db.transaction() as s:
        payloads = (await s.scalars(select(Message.payload).where(Message.session_id == session.id))).all()
    assert all("super-secret" not in p for p in payloads)
    calls = [i for i in to_items(await container.sessions.history(session.id)) if i["type"] == "tool_call"]
    assert "[redacted]" in calls[0]["text"]


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


async def test_webpush_isolates_subscriptions(container, monkeypatch):
    subs = container.push_subscriptions
    for name in ("gone", "bad", "ok", "busy"):
        await subs.add(f"https://push.example/{name}", "k", "a")
    statuses = {"gone": 410, "bad": 403, "ok": None, "busy": 503}
    sent: list[str] = []

    def fake_push(self, sub, data):
        name = sub.endpoint.rsplit("/", 1)[1]
        sent.append(name)
        return statuses[name]

    monkeypatch.setattr(WebPushDelivery, "_push", fake_push)
    delivery = WebPushDelivery(subs, container.vapid, "mailto:a@b.c")
    note = Notification(channel=DeliveryChannel.WEBPUSH, target=None, title="t", body="b", session_id=None)

    # 403 等其他 4xx 多半是本端问题：任务失败可见，但不删用户的订阅
    with pytest.raises(RuntimeError, match="403"):
        await delivery.send(note)

    assert sorted(sent) == ["bad", "busy", "gone", "ok"]
    assert sorted(s.endpoint.rsplit("/", 1)[1] for s in await subs.list()) == ["bad", "busy", "ok"]

    statuses["bad"] = None
    with pytest.raises(TransientError):
        await delivery.send(note)


async def test_telegram_notice_goes_through_deliver_task(container, make_agent):
    from muliao.triggers.telegram import TelegramTrigger

    await make_agent()
    trigger = TelegramTrigger(None, container.sessions, container.notifier, "alpha", {42})  # type: ignore[arg-type]
    await trigger.handle({"update_id": 1, "message": {"chat": {"id": 42}, "photo": []}})

    async with container.db.transaction() as s:
        tasks = (await s.scalars(select(Task))).all()
    assert [(t.kind, t.payload["target"], t.payload["channel"]) for t in tasks] == [
        (TaskKind.DELIVER, "42", DeliveryChannel.TELEGRAM)
    ]


async def test_retried_task_keeps_last_error_while_running(container, make_agent, models):
    await make_agent("alpha", model="m")

    def fail(_m, _i) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m")

    from tests.scripted import scripted

    models.models["m"] = scripted(fail)
    await start(container, "alpha", "hi", delivery=DeliveryChannel.NONE)
    await drain(container)
    turn = (await container.queue.list(status=TaskStatus.FAILED, session_id=None, limit=5))[0]
    assert turn.attempts == container.settings.task_max_attempts and "503" in turn.error

    models.models["m"] = scripted(lambda _m, _i: ModelResponse(parts=[TextPart("ok")]))
    await container.queue.retry(turn.id)
    claimed = await container.queue.claim()
    assert claimed.error is not None  # 运行期间仍能看到上次为什么失败


async def test_agent_referenced_by_config_cannot_be_deleted(settings, models):
    from muliao.agents import AgentSpec
    from muliao.container import build_container
    from muliao.db import init_schema

    settings.telegram_agent = "tg"
    c = build_container(settings, resolve_model=models, model_checker=lambda _m: None)
    await init_schema(c.db.engine)
    try:
        await c.agents.create("tg", AgentSpec(description="d", model="m", instructions="", reflect_every=5))
        with pytest.raises(Conflict, match="server configuration"):
            await c.agents.delete("tg")
    finally:
        await c.http.aclose()
        await c.db.engine.dispose()
