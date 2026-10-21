import asyncio
import json

import pytest
import requests

import muliao.notifications.webpush as webpush_module
from muliao.errors import TransientError
from muliao.notifications.delivery import DeliverHandler
from muliao.notifications.tables import Notification, PushSubscription
from muliao.notifications.webpush import WebPushDelivery
from muliao.platform.tables import Task, TaskKind
from tests.test_flow import drain, start, tasks_of


class RecordingPusher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int, int]] = []

    async def send(self, subscription: PushSubscription, notification: Notification, unread: int) -> None:
        self.sent.append((subscription.endpoint, notification.id, unread))


def _sub(name: str) -> PushSubscription:
    return PushSubscription(endpoint=f"https://push.example/{name}", p256dh="k", auth="a")


def _webpush(container) -> WebPushDelivery:
    return WebPushDelivery(container.push_subscriptions, container.vapid, "mailto:a@b.c")


async def _subscribe(container, *names: str) -> None:
    for name in names:
        await container.push_subscriptions.add(f"https://push.example/{name}", "k", "a")


async def test_push_carries_the_unread_count_and_system_notifications_open_the_notification_page(
    container, monkeypatch
):
    payloads: list[dict] = []
    monkeypatch.setattr(WebPushDelivery, "_push", lambda _self, _sub, data: payloads.append(json.loads(data)))

    await _webpush(container).send(
        _sub("a"), Notification(id=1, title="t", body="b", session_id=None), unread=3
    )

    assert payloads == [{"title": "t", "body": "b", "url": "/notifications", "session_id": None, "badge": 3}]


async def test_a_notification_is_one_deliver_task_per_device(container):
    await _subscribe(container, "phone", "laptop")

    await container.notifications.notify(None, "t", "b", None)

    tasks = await tasks_of(container, TaskKind.DELIVER)
    assert sorted(t.payload["endpoint"] for t in tasks) == [
        "https://push.example/laptop",
        "https://push.example/phone",
    ]
    # 设备之间不在同一条 lane 上：一台等重试不挡另一台；lane 会进日志，不带 endpoint（订阅凭据）
    assert len({t.lane for t in tasks}) == 2
    assert all("push.example" not in t.lane for t in tasks)


async def test_without_devices_the_notification_is_kept_but_nothing_is_delivered(container):
    async with container.bus.subscribe() as sub:
        await container.notifications.notify(None, "t", "b", None)
        # 前端靠这个事件刷新通知页与未读数，不靠投递任务：没开推送的浏览器也要看到新通知
        created = await asyncio.wait_for(sub.next(), 1)
    assert created.type == "notification"

    assert await tasks_of(container, TaskKind.DELIVER) == []
    assert [n.title for n in await container.notifications.list(10)] == ["t"]


async def test_retrying_a_temporarily_failed_device_does_not_push_the_others_again(container, monkeypatch):
    await _subscribe(container, "busy", "ok")
    reached: list[str] = []
    busy_status = {"value": 503}

    def fake_push(_self, sub, _data):  # noqa: ANN001, ANN202
        name = sub.endpoint.rsplit("/", 1)[1]
        reached.append(name)
        return busy_status["value"] if name == "busy" else None

    monkeypatch.setattr(WebPushDelivery, "_push", fake_push)
    handler = DeliverHandler(container.db, _webpush(container))
    await container.notifications.notify(None, "t", "b", None)
    tasks = {t.payload["endpoint"].rsplit("/", 1)[1]: t for t in await tasks_of(container, TaskKind.DELIVER)}

    await handler.execute(tasks["ok"])
    with pytest.raises(TransientError, match="503"):
        await handler.execute(tasks["busy"])
    # 任务队列重试的只是失败的那一台
    busy_status["value"] = None
    await handler.execute(tasks["busy"])

    assert sorted(reached) == ["busy", "busy", "ok"]


async def test_deliver_counts_the_notification_itself_as_unread(container):
    pusher = RecordingPusher()
    await _subscribe(container, "a")
    await container.notifications.notify(None, "t", "b", None)
    [note] = await container.notifications.list(10)
    [task] = await tasks_of(container, TaskKind.DELIVER)

    await DeliverHandler(container.db, pusher).execute(task)

    assert pusher.sent == [("https://push.example/a", note.id, 1)]


async def test_deliver_does_not_push_a_notification_deleted_with_its_session(container):
    pusher = RecordingPusher()
    await _subscribe(container, "a")
    task = Task(
        kind=TaskKind.DELIVER,
        payload={"notification_id": 404, "endpoint": "https://push.example/a"},
        session_id="gone",
    )

    await DeliverHandler(container.db, pusher).execute(task)

    assert pusher.sent == []


async def test_deliver_skips_a_device_unsubscribed_after_it_was_queued(container):
    pusher = RecordingPusher()
    await _subscribe(container, "a")
    await container.notifications.notify(None, "t", "b", None)
    [task] = await tasks_of(container, TaskKind.DELIVER)
    await container.push_subscriptions.remove("https://push.example/a")

    await DeliverHandler(container.db, pusher).execute(task)

    assert pusher.sent == []


async def test_push_statuses_decide_retry_failure_or_removal(container, monkeypatch):
    await _subscribe(container, "gone", "bad", "busy")
    statuses = {"gone": 410, "bad": 403, "busy": 503}
    monkeypatch.setattr(
        WebPushDelivery, "_push", lambda _self, sub, _data: statuses[sub.endpoint.rsplit("/", 1)[1]]
    )
    delivery = _webpush(container)
    note = Notification(id=1, title="t", body="b", session_id=None)

    # 404/410：订阅失效，删除，投递算完成
    await delivery.send(_sub("gone"), note, unread=1)
    # 其他 4xx 多半是本端问题：task 失败可见、不重试，也不删用户的订阅
    with pytest.raises(RuntimeError, match="403") as rejected:
        await delivery.send(_sub("bad"), note, unread=1)
    assert not isinstance(rejected.value, TransientError)
    with pytest.raises(TransientError, match="503"):
        await delivery.send(_sub("busy"), note, unread=1)

    assert sorted(s.endpoint.rsplit("/", 1)[1] for s in await container.push_subscriptions.list()) == [
        "bad",
        "busy",
    ]


async def test_any_transport_failure_is_retried(container, monkeypatch):
    def fake_webpush(subscription_info, **_kwargs):  # noqa: ANN001, ANN202
        raise requests.exceptions.ChunkedEncodingError("connection broken mid-response")

    monkeypatch.setattr(webpush_module, "webpush", fake_webpush)

    with pytest.raises(TransientError):
        await _webpush(container).send(
            _sub("a"), Notification(id=1, title="t", body="b", session_id=None), unread=1
        )


async def test_a_local_request_error_is_not_retried_as_transient(container, monkeypatch):
    """URL、请求头不合法这类本端问题重试也不会好，照常抛出，task 失败可见。"""

    def fake_webpush(subscription_info, **_kwargs):  # noqa: ANN001, ANN202
        raise requests.exceptions.InvalidHeader("bad header value")

    monkeypatch.setattr(webpush_module, "webpush", fake_webpush)
    with pytest.raises(requests.exceptions.InvalidHeader):
        await _webpush(container).send(
            _sub("a"), Notification(id=1, title="t", body="b", session_id=None), unread=1
        )


async def test_deleting_a_session_deletes_the_notifications_pointing_to_it(container, make_agent):
    alpha = (await make_agent("alpha")).id
    session = await start(container, alpha, "hi")
    await drain(container)
    await container.notifications.notify(None, "system", "b", None)
    assert await container.notifications.unread_count() == 2

    await container.sessions.delete(session.id)
    # 删除的清理经事件完成
    await drain(container)

    assert [n.title for n in await container.notifications.list(10)] == ["system"]
