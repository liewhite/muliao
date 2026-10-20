import json

from muliao.delivery.base import DeliverHandler
from muliao.delivery.webpush import WebPushDelivery
from muliao.models import Notification, Task, TaskKind
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

    assert [n.title for n in await container.notifications.list(10)] == ["system"]
