import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from muliao.api import admin, agents, sessions
from muliao.api.auth import COOKIE_NAME, MAX_AGE_SECONDS, Auth, require_auth
from muliao.api.deps import C
from muliao.bus import SubscriberOverflow
from muliao.container import Container
from muliao.errors import Conflict, DomainError, Forbidden, InvalidInput, NotFound
from muliao.triggers.webhook import MAX_EVENT_BYTES

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0
_STATUS = {NotFound: 404, Conflict: 409, InvalidInput: 422, Forbidden: 403}


class LoginIn(BaseModel):
    password: str


class SessionIdOut(BaseModel):
    session_id: str


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

    @router.post("/hooks/{hook_id}", status_code=202)
    async def receive_webhook(
        c: C, hook_id: str, request: Request, x_muliao_token: str = Header(...)
    ) -> SessionIdOut:
        declared = int(request.headers.get("content-length", "0"))
        if declared > MAX_EVENT_BYTES:
            raise InvalidInput(f"event body exceeds {MAX_EVENT_BYTES} bytes")
        raw = await request.body()
        if len(raw) > MAX_EVENT_BYTES:
            raise InvalidInput(f"event body exceeds {MAX_EVENT_BYTES} bytes")
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise InvalidInput(f"event body must be UTF-8: {e}") from e
        session = await c.webhooks.receive(hook_id, x_muliao_token, body)
        return SessionIdOut(session_id=session.id)

    return router


def _private_routes() -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
    router.include_router(agents.router)
    router.include_router(sessions.router)
    router.include_router(admin.router)

    @router.get("/auth/me", status_code=204)
    async def me() -> None:
        return None

    @router.get("/events")
    async def events(c: C) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            async with c.bus.subscribe() as sub:
                it = aiter(sub)
                yield "retry: 3000\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(anext(it), timeout=_HEARTBEAT_SECONDS)
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
        if path.startswith(("api/", "hooks/")):
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
