import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from muliao.api import admin, agents, files, notifications, resources, sessions, triggers, webhook
from muliao.api.auth import COOKIE_NAME, MAX_AGE_SECONDS, Auth, require_auth
from muliao.api.deps import C
from muliao.api.events import HeartbeatEvent, ServerEvent
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
from muliao.platform.bus import SubscriberOverflow
from muliao.triggers.precheck import PrecheckFailed

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0
TITLE = "Muliao"


def _heartbeat(ui_version: str | None) -> HeartbeatEvent:
    """心跳是一个真正的事件（不是 SSE 注释行，那种前端读不到），连上就发一次、之后每隔一段时间发一次：
    - 前端据此判断连接是否还活着：手机切后台、代理链路断开时 EventSource 往往收不到任何错误，只能靠「多久没收到
      消息」发现；间隔随心跳下发，前端不另存一份；
    - ui_version 是服务端当前前端构建的版本：手机主屏应用常驻内存，升级后旧页面还在跑，前端据此提示刷新。"""
    return HeartbeatEvent(interval_seconds=_HEARTBEAT_SECONDS, ui_version=ui_version)


def ui_version(web_dir: Path) -> str | None:
    """前端构建的版本：index.html 引用的资源文件名带内容哈希，它的内容变了就是换了构建。没有构建前端时为 None。"""
    index = web_dir / "index.html"
    return hashlib.sha256(index.read_bytes()).hexdigest()[:12] if index.exists() else None


_STATUS = {
    NotFound: 404,
    Conflict: 409,
    InvalidInput: 422,
    Unauthorized: 401,
    Forbidden: 403,
    TooLarge: 413,
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


def _public_routes() -> APIRouter:
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
            secure=request.app.state.secure_cookie,
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

    # FastAPI 的 SSE：每条 data 按 ServerEvent 校验并序列化，OpenAPI 里登记为事件流的条目类型，前端类型由此生成。
    # 断线重连的间隔用浏览器的默认值（几秒），连接是否还活着由心跳与前端的看门狗判断。FastAPI 自己空闲时发的
    # `: ping` 注释行只防代理断开，EventSource 不把注释交给页面，所以心跳仍然需要
    @router.get("/events", response_class=EventSourceResponse)
    async def events(request: Request, c: C) -> AsyncIterator[ServerEvent]:
        heartbeat = _heartbeat(request.app.state.ui_version)
        async with c.bus.subscribe() as sub:
            # 连上就先发一次心跳：前端立刻知道间隔
            yield heartbeat
            while True:
                try:
                    with anyio.move_on_after(_HEARTBEAT_SECONDS) as waited:
                        event = await sub.next()
                except SubscriberOverflow:
                    return
                yield heartbeat if waited.cancelled_caught else event

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
        async with container.running():
            yield

    app = FastAPI(title=TITLE, lifespan=lifespan)
    settings = container.settings
    app.state.container = container
    app.state.auth = Auth(
        settings.auth.secret_key.get_secret_value(), settings.auth.password.get_secret_value()
    )

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=_status_of(exc), content={"detail": str(exc)})

    @app.exception_handler(UpstreamFailed)
    async def upstream_failed(_request: Request, exc: UpstreamFailed) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    app.state.secure_cookie = settings.server.public_url.startswith("https://")
    app.state.ui_version = ui_version(web_dir)
    _install_api(app)
    _mount_web(app, web_dir)
    return app


def _install_api(app: FastAPI) -> None:
    """全部接口；不依赖运行时的 container，生成接口描述时也用它（api_schema）。处理请求时路由读取 app.state 上的
    container、auth、secure_cookie、ui_version，只有 create_app 设置它们：只装了路由的应用只能用来生成描述。"""
    app.include_router(_public_routes())
    # webhook 的事件入口凭触发器 token 鉴权，不走登录
    app.include_router(webhook.public_router)
    app.include_router(_private_routes())


def api_schema() -> dict[str, Any]:
    """接口的 OpenAPI 描述（含 SSE 事件），与运行中的服务一致；web/openapi.json 由它生成。"""
    app = FastAPI(title=TITLE)
    _install_api(app)
    return app.openapi()


if __name__ == "__main__":
    # uv run python -m muliao.api.app > web/openapi.json
    print(json.dumps(api_schema(), ensure_ascii=False, indent=2))
