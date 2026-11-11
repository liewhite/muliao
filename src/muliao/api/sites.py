"""agent 自定义页面（#355，工作区领域 Sites）：界面列出各 agent 的页面，iframe 打开。

页面是 agent 写的 HTML + 脚本，要能执行，又不能与界面同源（同源就带着登录 cookie，能以用户身份调全部接口，
docs/architecture.md 第 15 节的不可信内容约定）。所以页面响应带 `Content-Security-Policy: sandbox allow-scripts …`
（不含 allow-same-origin）：脚本照常跑，但页面是一个不透明的源，读不到 cookie、调不了 /api。沙箱里的请求不带 cookie，
页面及其同目录文件改凭签名 URL 访问：`/sites/<agent>/<页面>/<签名>/<文件>`，签名只有已登录的界面从
`GET /api/pages` 拿得到（api/auth.py `sign_site`）。页面里用相对路径读同目录的数据（`fetch('data.json')`）：那是
从不透明的源发出的跨源请求，所以响应带 `Access-Control-Allow-Origin: *`；`Referrer-Policy: no-referrer` 免得页面
加载外部资源时把签名 URL 带出去。内容随时会变，不缓存。
"""

from datetime import datetime
from urllib.parse import quote

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from muliao.api.auth import Auth
from muliao.api.deps import C, reads

router = APIRouter()
public_router = APIRouter()

SITES_PATH = "/sites"

# 沙箱权限与前端 iframe 的 sandbox 属性（web/src/pages/SitePages.tsx SANDBOX）一致：浏览器取两者交集，改就两处一起改。
# 签名不过期、按页面签：地址可以收藏、在新窗口常开；泄露了改密码即失效。
_PAGE_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads",
    "X-Content-Type-Options": "nosniff",
    "Access-Control-Allow-Origin": "*",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class SitePageOut(BaseModel):
    agent_id: str
    # 页面目录名（site/<name>/）
    name: str
    # index.html 的 <title>，没有就是 name
    title: str
    # 页面目录里最近改动的文件的时间（数据文件刷新也算）
    modified: datetime
    # 打开页面的签名地址（以 / 结尾，页面里的相对路径据此解析）
    url: str


def _site_url(auth: Auth, agent_id: str, page: str) -> str:
    return f"{SITES_PATH}/{agent_id}/{quote(page, safe='')}/{auth.sign_site(agent_id, page)}/"


@router.get("/pages", openapi_extra=reads("agents"))
async def list_pages(request: Request, c: C) -> list[SitePageOut]:
    """全部 agent 的页面：按 agent 列表的顺序，各自按页面名排序。"""
    auth: Auth = request.app.state.auth
    out = []
    for agent in await c.agents.list():
        pages = await anyio.to_thread.run_sync(c.sites.pages, agent.id)
        out += [
            SitePageOut(
                agent_id=agent.id,
                name=p.name,
                title=p.title,
                modified=p.modified,
                url=_site_url(auth, agent.id, p.name),
            )
            for p in pages
        ]
    return out


def _check(request: Request, agent_id: str, page: str, sig: str) -> None:
    auth: Auth = request.app.state.auth
    if not auth.verify_site(agent_id, page, sig):
        raise HTTPException(status_code=404)


@public_router.get(SITES_PATH + "/{agent_id}/{page}/{sig}", include_in_schema=False)
async def page_root(request: Request, agent_id: str, page: str, sig: str) -> RedirectResponse:
    """不带结尾斜杠时页面里的相对路径会解析到上一级，补上。"""
    _check(request, agent_id, page, sig)
    return RedirectResponse(_site_url(request.app.state.auth, agent_id, page), status_code=307)


@public_router.get(SITES_PATH + "/{agent_id}/{page}/{sig}/{path:path}", include_in_schema=False)
async def page_file(request: Request, c: C, agent_id: str, page: str, sig: str, path: str) -> FileResponse:
    _check(request, agent_id, page, sig)
    await c.agents.get_active(agent_id)
    f = await anyio.to_thread.run_sync(c.sites.file, agent_id, page, path)
    return FileResponse(f.path, media_type=f.media_type, headers=_PAGE_HEADERS)
