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
from muliao.models import CronJob, Message, Origin, TaskKind, TaskStatus
from muliao.runtime.worker import is_transient
from muliao.transcript import to_items
from tests.test_flow import call_then_answer, drain, start


async def test_message_ids_never_reused_after_deleting_latest_session(container, make_agent):
    alpha = (await make_agent("alpha", reflect_every=100)).id
    first = await start(container, alpha, "one", origin=Origin.CRON)
    await drain(container)
    async with container.db.transaction() as s:
        max_before = max((await s.scalars(select(Message.id))).all())
    await container.sessions.delete(first.id)

    await start(container, alpha, "two", origin=Origin.CRON)
    await drain(container)

    async with container.db.transaction() as s:
        ids = (await s.scalars(select(Message.id))).all()
    assert min(ids) > max_before


async def test_next_due_ignores_tasks_blocked_by_running_lane(container, make_agent):
    alpha = (await make_agent()).id
    async with container.db.transaction() as s:
        for _ in range(2):
            container.queue.enqueue(
                s, kind=TaskKind.DELIVER, lane="x", agent_id=alpha, session_id=None, payload={}
            )
    await container.queue.claim()

    # 第二个 task 被同 lane 的 running 挡住，不应让 worker 以为马上有活可干
    assert await container.queue.next_due() is None


async def test_deleting_session_or_agent_with_running_task_is_rejected(container, make_agent):
    alpha = (await make_agent()).id
    session = await start(container, alpha, "hi")
    await container.queue.claim()

    with pytest.raises(Conflict):
        await container.sessions.delete(session.id)
    with pytest.raises(Conflict):
        await container.agents.delete(alpha)


async def test_broken_cron_job_is_disabled_without_blocking_others(container, make_agent):
    alpha = (await make_agent()).id
    bad = await container.cron.create(name="bad", agent_id=alpha, schedule="* * * * *", prompt="x")
    good = await container.cron.create(name="good", agent_id=alpha, schedule="* * * * *", prompt="y")
    async with container.db.transaction() as s:
        for job_id in (bad.id, good.id):
            (await s.get(CronJob, job_id)).next_run_at = datetime.now().astimezone() - timedelta(seconds=1)
        (await s.get(CronJob, bad.id)).prompt = "   "  # 模拟绕过校验写入的坏数据

    await container.cron.fire_due()

    bad, good = await container.cron.get_by_name("bad"), await container.cron.get_by_name("good")
    assert not bad.enabled and "message must not be empty" in bad.last_error
    assert good.last_session_id is not None


async def test_agent_api_rejects_zero_reflect_every_and_partial_update(container, tmp_path):
    from muliao.api.app import create_app
    from tests.conftest import login

    app = create_app(container, tmp_path / "no-web")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        await login(client)
        resp = await client.post("/api/agents", json={"name": "a", "description": "d", "reflect_every": 0})
        assert resp.status_code == 422
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
    session = await start(container, alpha, "save my token", origin=Origin.CRON)
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
    note = Notification(title="t", body="b", session_id=None)

    # 403 等其他 4xx 多半是本端问题：任务失败可见，但不删用户的订阅
    with pytest.raises(RuntimeError, match="403"):
        await delivery.send(note)

    assert sorted(sent) == ["bad", "busy", "gone", "ok"]
    assert sorted(s.endpoint.rsplit("/", 1)[1] for s in await subs.list()) == ["bad", "busy", "ok"]

    statuses["bad"] = None
    with pytest.raises(TransientError):
        await delivery.send(note)


async def test_retried_task_keeps_last_error_while_running(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id

    def fail(_m, _i) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m")

    from tests.scripted import scripted

    models.models["m"] = scripted(fail)
    await start(container, alpha, "hi", origin=Origin.CRON)
    await drain(container)
    turn = (await container.queue.list(status=TaskStatus.FAILED, session_id=None, limit=5))[0]
    assert turn.attempts == container.settings.runtime.task_max_attempts and "503" in turn.error

    models.models["m"] = scripted(lambda _m, _i: ModelResponse(parts=[TextPart("ok")]))
    await container.queue.retry(turn.id)
    claimed = await container.queue.claim()
    assert claimed.error is not None  # 运行期间仍能看到上次为什么失败


def test_vapid_private_key_is_owner_only(tmp_path):
    from muliao.delivery.webpush import VapidKeys

    path = tmp_path / "vapid.pem"
    VapidKeys(path)
    assert path.stat().st_mode & 0o777 == 0o600

    path.chmod(0o644)  # 旧版本生成的宽权限文件
    VapidKeys(path)
    assert path.stat().st_mode & 0o777 == 0o600


async def test_subscriber_survives_heartbeat_timeouts():
    import asyncio

    from muliao.bus import EventBus

    bus = EventBus()
    async with bus.subscribe() as sub:
        for _ in range(3):  # SSE 每次心跳都是一次超时
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(sub.next(), timeout=0.01)
        bus.publish({"type": "x"})
        assert await asyncio.wait_for(sub.next(), timeout=1) == {"type": "x"}
