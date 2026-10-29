"""agent 工作目录的只读浏览（工作区领域 WorkspaceFiles）：agent 页「文件」列出一层目录、读一个文件。

文件内容是 agent 写的，与界面同源、带着登录 cookie，所以和对话里的文件下载（api/files.py）一样按不可信处理：
`nosniff` 加 `CSP: sandbox`，只有 INLINE_IMAGE_TYPES 里的图片按 inline 返回，其余一律作为附件，不在本站执行；
界面预览文本用 fetch 读内容，不受 attachment 影响。内容随时会变，不缓存。
"""

import mimetypes
from datetime import datetime

import anyio
from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.files.api import INLINE_IMAGE_TYPES
from muliao.workspace.api import Entry

router = APIRouter()

_SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
    "Cache-Control": "no-cache",
}


class EntryOut(BaseModel):
    name: str
    is_dir: bool
    # 目录为 null
    size: int | None
    modified: datetime

    @classmethod
    def of(cls, e: Entry) -> "EntryOut":
        return cls(name=e.name, is_dir=e.is_dir, size=e.size, modified=e.modified)


@router.get("/agents/{agent_id}/workspace")
async def list_workspace(c: C, agent_id: str, path: str = "") -> list[EntryOut]:
    """列出工作目录里 path（相对工作目录，空为根）这一层：目录在前，各自按名称排序。"""
    await c.agents.get(agent_id)
    entries = await anyio.to_thread.run_sync(c.workspace_files.list, agent_id, path)
    return [EntryOut.of(e) for e in entries]


@router.get("/agents/{agent_id}/workspace/file")
async def read_workspace_file(c: C, agent_id: str, path: str) -> FileResponse:
    """工作目录里一个文件的内容。"""
    await c.agents.get(agent_id)
    target = await anyio.to_thread.run_sync(c.workspace_files.file, agent_id, path)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    inline = media_type in INLINE_IMAGE_TYPES
    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
        content_disposition_type="inline" if inline else "attachment",
        headers=_SAFE_HEADERS,
    )
