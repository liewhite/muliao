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

from muliao.api import admin, agents, prompts, sessions, triggers
from muliao.api.auth import COOKIE_NAME, MAX_AGE_SECONDS, Auth, require_auth
from muliao.api.deps import C
from muliao.bus import SubscriberOverflow
from muliao.container import Container
from muliao.errors import Conflict, DomainError, Forbidden, InvalidInput, NotFound, TooLarge, Unauthorized
from muliao.triggers.service import EVENT_MAX_BYTES

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0
_STATUS = {
    NotFound: 404,
    Conflict: 409,
    InvalidInput: 422,
    Unauthorized: 401,
    Forbidden: 403,
    TooLarge: 413,
}


class LoginIn(BaseModel):
    password: str


class HookOut(BaseModel):
    session_id: str


def _status_of(exc: DomainError) -> int:
    for cls, status in _STATUS.items():
        if isinstance(exc, cls):
            return status
    raise TypeError(f"unmapped domain error {type(exc).__name__}") from exc


async def _read_limited(request: Request, limit: int) -> bytes:
    """按字节上限读取请求体：声明的长度超限直接拒绝，未声明长度的分块读取，超限即停。"""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise TooLarge(f"request body exceeds {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise TooLarge(f"request body exceeds {limit} bytes")
    return bytes(body)


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

    @router.post("/api/hooks/{name}", status_code=202)
    async def hook(name: str, request: Request, session_id: str | None = None) -> HookOut:
        """外部事件入口：凭触发器 token（Authorization: Bearer <token>）鉴权，不走登录 cookie。

        不带 session_id 时新建会话；带上之前返回的 session_id 则在该会话里继续。
        """
        c: Container = request.app.state.container
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthorized("missing bearer token")
        # 先鉴权再读请求体：未鉴权的调用方不能让进程读入任何内容
        trigger_id = await c.webhooks.authenticate(name, token)
        payload = await _read_limited(request, EVENT_MAX_BYTES)
        try:
            text = payload.decode()
        except UnicodeDecodeError as e:
            raise InvalidInput("event payload must be UTF-8 text") from e
        session = await c.webhooks.deliver(trigger_id, text, session_id)
        return HookOut(session_id=session.id)

    @router.post("/api/auth/logout", status_code=204)
    async def logout(response: Response) -> None:
        response.delete_cookie(COOKIE_NAME)

    return router


def _private_routes() -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
    router.include_router(agents.router)
    router.include_router(sessions.router)
    router.include_router(admin.router)
    router.include_router(prompts.router)
    router.include_router(triggers.router)

    @router.get("/auth/me", status_code=204)
    async def me() -> None:
        return None

    @router.get("/events")
    async def events(c: C) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            async with c.bus.subscribe() as sub:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(sub.next(), timeout=_HEARTBEAT_SECONDS)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
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
    app.include_router(_private_routes())
    _mount_web(app, web_dir)
    return app
