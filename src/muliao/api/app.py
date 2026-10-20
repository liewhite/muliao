import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from muliao.api import admin, agents, files, notifications, resources, sessions, triggers, webhook
from muliao.api.auth import COOKIE_NAME, MAX_AGE_SECONDS, Auth, require_auth
from muliao.api.deps import C
from muliao.bus import SubscriberOverflow
from muliao.container import Container
from muliao.errors import (
    Conflict,
    DomainError,
    Forbidden,
    InvalidInput,
    NotFound,
    TooLarge,
    Unauthorized,
    UpstreamFailed,
)
from muliao.triggers.precheck import PrecheckFailed

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0
# 心跳是一个真正的事件（不是 SSE 注释行，那种前端读不到）：前端据此判断连接是否还活着。
# 手机切后台、代理链路断开时 EventSource 往往收不到任何错误，只能靠「多久没收到消息」发现；间隔随心跳下发，前端不另存一份
_HEARTBEAT = f"data: {json.dumps({'type': 'heartbeat', 'interval_seconds': _HEARTBEAT_SECONDS})}\n\n"
_STATUS = {
    NotFound: 404,
    Conflict: 409,
    InvalidInput: 422,
    Unauthorized: 401,
    Forbidden: 403,
    TooLarge: 413,
    UpstreamFailed: 502,
    # 手动运行触发器时前置检查失败：原因（可能含脚本 stderr）给已登录的用户看
    PrecheckFailed: 502,
}


class LoginIn(BaseModel):
    password: str


def _status_of(exc: DomainError) -> int:
    for cls, status in _STATUS.items():
        if isinstance(exc, cls):
            return status
    raise TypeError(f"unmapped domain error {type(exc).__name__}") from exc


def _public_routes(secure_cookie: bool) -> APIRouter:
    router = APIRouter()

    @router.post("/api/auth/login", status_code=204)
    async def login(request: Request, body: LoginIn, response: Response) -> None:
        auth: Auth = request.app.state.auth
        if not auth.check_password(body.password):
            log.warning("login failed", extra={"client": request.client.host if request.client else None})
            raise HTTPException(status_code=401, detail="wrong password")
        response.set_cookie(
            COOKIE_NAME,
            auth.issue(),
            max_age=MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=secure_cookie,
        )

    @router.post("/api/auth/logout", status_code=204)
    async def logout(response: Response) -> None:
        response.delete_cookie(COOKIE_NAME)

    return router


def _private_routes() -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
    router.include_router(agents.router)
    router.include_router(sessions.router)
    router.include_router(admin.router)
    router.include_router(resources.router)
    router.include_router(triggers.router)
    router.include_router(webhook.router)
    router.include_router(files.router)
    router.include_router(notifications.router)

    @router.get("/auth/me", status_code=204)
    async def me() -> None:
        return None

    @router.get("/events")
    async def events(c: C) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            async with c.bus.subscribe() as sub:
                # 连上就先发一次心跳：前端立刻知道间隔
                yield "retry: 3000\n\n" + _HEARTBEAT
                while True:
                    try:
                        event = await asyncio.wait_for(sub.next(), timeout=_HEARTBEAT_SECONDS)
                    except TimeoutError:
                        yield _HEARTBEAT
                        continue
                    except SubscriberOverflow:
                        return
                    yield f"data: {json.dumps(event, default=str)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def _mount_web(app: FastAPI, web_dir: Path) -> None:
    index = web_dir / "index.html"
    if not index.exists():
        log.warning("web UI not built, serving API only", extra={"web_dir": str(web_dir)})
        return
    app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")
    root = web_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (web_dir / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def create_app(container: Container, web_dir: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await container.start()
        try:
            yield
        finally:
            await container.stop()

    app = FastAPI(title="Muliao", lifespan=lifespan)
    settings = container.settings
    app.state.container = container
    app.state.auth = Auth(
        settings.auth.secret_key.get_secret_value(), settings.auth.password.get_secret_value()
    )

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=_status_of(exc), content={"detail": str(exc)})

    app.include_router(_public_routes(secure_cookie=settings.server.public_url.startswith("https://")))
    # webhook 的事件入口凭触发器 token 鉴权，不走登录
    app.include_router(webhook.public_router)
    app.include_router(_private_routes())
    _mount_web(app, web_dir)
    return app
