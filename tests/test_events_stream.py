"""/api/events 的 SSE：连上先发心跳，之后总线上的事件按 ServerEvent 的形状逐条发出。"""

import asyncio
import json

from muliao.api.app import create_app
from muliao.events import SavedEvent
from tests.conftest import login


async def _read_events(app, cookie: str, count: int, after_connect) -> list[dict]:
    """直接驱动 ASGI 应用读一个永不结束的事件流（httpx 的 ASGI transport 要等响应体结束），读够 count 条就断开。"""
    events: list[dict] = []
    status: list[int] = []
    disconnect = asyncio.Event()
    buffer = ""

    async def receive():
        if not hasattr(receive, "sent"):
            receive.sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal buffer
        if message["type"] == "http.response.start":
            status.append(message["status"])
            assert dict(message["headers"])[b"content-type"].startswith(b"text/event-stream")
        elif message["type"] == "http.response.body":
            buffer += message.get("body", b"").decode()
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                data = [line[len("data: ") :] for line in block.splitlines() if line.startswith("data: ")]
                if data:
                    events.append(json.loads("\n".join(data)))
                    if len(events) == 1:
                        after_connect()
                    if len(events) >= count:
                        disconnect.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"cookie", cookie.encode())],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    assert status == [200]
    return events


async def test_the_event_stream_sends_a_heartbeat_then_bus_events(client, container, tmp_path):
    await login(client)
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    app = create_app(container, tmp_path / "no-web")

    events = await _read_events(app, cookie, 2, lambda: container.bus.publish(SavedEvent(session_id="s1")))

    heartbeat, saved = events
    assert heartbeat["type"] == "heartbeat" and heartbeat["interval_seconds"] > 0
    assert saved == {"type": "saved", "session_id": "s1"}
