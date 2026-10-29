"""agent 工作目录的只读浏览（工作区领域 WorkspaceFiles）：agent 页「文件」列出一层目录、读一个文件。

文件内容是 agent 写的，与界面同源、带着登录 cookie，所以和对话里的文件下载（api/files.py）一样按不可信处理
（files.UNTRUSTED_CONTENT_HEADERS），只有能内联的图片按 inline 返回，其余一律作为附件，不在本站执行；
界面预览文本用 fetch 读内容，不受 attachment 影响。内容随时会变，不缓存。
"""

from datetime import datetime

import anyio
from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.files.api import UNTRUSTED_CONTENT_HEADERS
from muliao.workspace.api import Entry

router = APIRouter()

_SAFE_HEADERS = {**UNTRUSTED_CONTENT_HEADERS, "Cache-Control": "no-cache"}


class EntryOut(BaseModel):
    name: str
    is_dir: bool
    # 目录为 null
    size: int | None
    modified: datetime
    # 能内联显示的图片；其余读文件时作为附件返回
    inline: bool

    @classmethod
    def of(cls, e: Entry) -> "EntryOut":
        return cls(name=e.name, is_dir=e.is_dir, size=e.size, modified=e.modified, inline=e.inline)


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
    f = await anyio.to_thread.run_sync(c.workspace_files.file, agent_id, path)
    return FileResponse(
        f.path,
        media_type=f.media_type,
        filename=f.path.name,
        content_disposition_type="inline" if f.inline else "attachment",
        headers=_SAFE_HEADERS,
    )
