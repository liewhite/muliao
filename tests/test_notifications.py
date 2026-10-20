import json

import pytest

from muliao.notifications.delivery import DeliverHandler
from muliao.notifications.tables import Notification
from muliao.notifications.webpush import WebPushDelivery
from muliao.platform.tables import Task, TaskKind
from tests.test_flow import drain, start


class RecordingPusher:
    def __init__(self) -> None:
        self.sent: list[tuple[Notification, int]] = []

    async def send(self, notification: Notification, unread: int) -> None:
        self.sent.append((notification, unread))


async def test_push_carries_the_unread_count_and_system_notifications_open_the_notification_page(
    container, monkeypatch
):
    await container.push_subscriptions.add("https://push.example/a", "k", "a")
    payloads: list[dict] = []
    monkeypatch.setattr(WebPushDelivery, "_push", lambda _self, _sub, data: payloads.append(json.loads(data)))
    delivery = WebPushDelivery(container.push_subscriptions, container.vapid, "mailto:a@b.c")

    await delivery.send(Notification(id=1, title="t", body="b", session_id=None), unread=3)

    assert payloads == [{"title": "t", "body": "b", "url": "/notifications", "session_id": None, "badge": 3}]


async def test_deliver_counts_the_notification_itself_as_unread(container):
    pusher = RecordingPusher()
    await container.notifier.notify(None, Notification(title="t", body="b", session_id=None))
    [note] = await container.notifications.list(10)
    task = Task(kind=TaskKind.DELIVER, payload={"notification_id": note.id}, session_id=None)

    await DeliverHandler(container.db, pusher).execute(task)

    assert [(n.id, unread) for n, unread in pusher.sent] == [(note.id, 1)]


async def test_deliver_does_not_push_a_notification_deleted_with_its_session(container):
    pusher = RecordingPusher()
    task = Task(kind=TaskKind.DELIVER, payload={"notification_id": 404}, session_id="gone")

    await DeliverHandler(container.db, pusher).execute(task)

    assert pusher.sent == []


async def test_deleting_a_session_deletes_the_notifications_pointing_to_it(container, make_agent):
    alpha = (await make_agent("alpha")).id
    session = await start(container, alpha, "hi")
    await drain(container)
    await container.notifier.notify(None, Notification(title="system", body="b", session_id=None))
    assert await container.notifications.unread_count() == 2

    await container.sessions.delete(session.id)
    # 删除的清理经事件完成
    await drain(container)

    assert [n.title for n in await container.notifications.list(10)] == ["system"]


async def test_any_transport_failure_is_retried_and_does_not_stop_other_devices(container, monkeypatch):
    import requests

    import muliao.notifications.webpush as webpush_module
    from muliao.errors import TransientError

    for name in ("broken", "ok"):
        await container.push_subscriptions.add(f"https://push.example/{name}", "k", "a")
    reached: list[str] = []

    def fake_webpush(subscription_info, **_kwargs):  # noqa: ANN001, ANN202
        reached.append(subscription_info["endpoint"])
        if subscription_info["endpoint"].endswith("broken"):
            raise requests.exceptions.ChunkedEncodingError("connection broken mid-response")

    monkeypatch.setattr(webpush_module, "webpush", fake_webpush)
    delivery = WebPushDelivery(container.push_subscriptions, container.vapid, "mailto:a@b.c")

    with pytest.raises(TransientError):
        await delivery.send(Notification(id=1, title="t", body="b", session_id=None), unread=1)
    assert len(reached) == 2


async def test_a_local_request_error_is_not_retried_as_transient(container, monkeypatch):
    """URL、请求头不合法这类本端问题重试也不会好；当成暂时性失败会让其他设备把同一条通知收到好几次。"""
    import requests

    import muliao.notifications.webpush as webpush_module

    await container.push_subscriptions.add("https://push.example/a", "k", "a")

    def fake_webpush(subscription_info, **_kwargs):  # noqa: ANN001, ANN202
        raise requests.exceptions.InvalidHeader("bad header value")

    monkeypatch.setattr(webpush_module, "webpush", fake_webpush)
    delivery = WebPushDelivery(container.push_subscriptions, container.vapid, "mailto:a@b.c")
    with pytest.raises(requests.exceptions.InvalidHeader):
        await delivery.send(Notification(id=1, title="t", body="b", session_id=None), unread=1)
