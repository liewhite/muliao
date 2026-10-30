"""/api/events 的 SSE：连上先发心跳，之后总线上的事件按 ServerEvent 的形状逐条发出。"""

import asyncio
import json
import logging

from muliao.sessions.live import SavedEvent
from tests.conftest import app_of, login


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
    app = app_of(container, tmp_path / "no-web")

    events = await _read_events(app, cookie, 2, lambda: container.bus.publish(SavedEvent(session_id="s1")))

    heartbeat, saved = events
    assert heartbeat["type"] == "heartbeat" and heartbeat["interval_seconds"] > 0
    assert saved == {"type": "saved", "session_id": "s1"}


async def test_closing_the_bus_ends_the_event_stream(client, container, tmp_path):
    """服务关停时关总线：客户端不断开，响应也自己结束（否则 uvicorn 到时强行取消并打 ERROR，#330）。"""
    await login(client)
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    app = app_of(container, tmp_path / "no-web")

    events = await _read_events(app, cookie, 99, container.bus.close)

    assert [e["type"] for e in events] == ["heartbeat"]


async def test_server_shutdown_ends_open_event_streams_without_errors(client, container, tmp_path, caplog):
    """服务入口的 _Server：有界面开着 SSE 时关停，连接在排空期内结束，不走超时取消，不打 ERROR。"""
    import httpx
    import uvicorn

    from muliao.__main__ import _Server

    await login(client)
    cookies = dict(client.cookies.items())
    app = app_of(container, tmp_path / "no-web")
    # lifespan 由夹具管；排空上限设得很长，连接要是没自己结束，测试会超时而不是被取消后通过
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", log_config=None, timeout_graceful_shutdown=60
    )
    server = _Server(config, container.bus)
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(5):
        while not server.started:  # noqa: ASYNC110 uvicorn 只有这个标志，没有可等待的启动事件
            assert not serving.done(), "server failed to start"
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", cookies=cookies) as http:
        async with http.stream("GET", "/api/events") as resp:
            lines = resp.aiter_lines()
            assert (await anext(lines)).startswith("data: ")  # 连上先发的心跳
            server.should_exit = True
            async with asyncio.timeout(5):
                await serving
                rest = [line async for line in lines]  # 服务端已结束响应，读到尾

    assert not [line for line in rest if line.startswith("data: ")]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
