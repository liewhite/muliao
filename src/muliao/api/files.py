"""对话里的文件：上传（先上传拿到 id，发消息时引用）与下载。做法同 Matrix 的媒体接口。

下载与界面同源、带着登录 cookie，所以内容一律按不可信处理：`nosniff` 加 `CSP: sandbox`，
只有 INLINE_IMAGE_TYPES 里的图片按 inline 返回，其余（HTML、SVG……）一律作为附件下载，不在本站执行。
"""

from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.errors import InvalidInput, NotFound
from muliao.files.api import FILES_PATH, FileMeta

router = APIRouter()

_SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
    # 内容按 id 永不改变
    "Cache-Control": "private, max-age=31536000, immutable",
}


class FileOut(BaseModel):
    id: str
    name: str
    media_type: str
    size: int
    url: str
    # 能内联显示的图片；其余是下载链接
    inline: bool
    created_at: datetime

    @classmethod
    def of(cls, f: FileMeta) -> "FileOut":
        return cls(
            id=f.id,
            name=f.name,
            media_type=f.media_type,
            size=f.size,
            url=f.url,
            inline=f.is_inline_image,
            created_at=f.created_at,
        )


_PREFIX = FILES_PATH.removeprefix("/api")


@router.post(_PREFIX, status_code=201)
async def upload(c: C, request: Request, name: str = Query(min_length=1)) -> FileOut:
    """请求体就是文件内容，Content-Type 是它的类型（与 Matrix /media/upload 相同，不用 multipart），边读边写，超限即停。"""
    media_type = request.headers.get("content-type")
    if not media_type:
        raise InvalidInput("Content-Type header is required: it is the type of the uploaded file")
    return FileOut.of(await c.files.put(name, media_type, request.stream()))


@router.get(_PREFIX + "/{file_id}/{name}")
async def download(c: C, file_id: str, name: str) -> FileResponse:
    meta = await c.files.get(file_id)
    # 每个文件只有一个规范地址
    if name != meta.name:
        raise NotFound(f"file {file_id!r} is not named {name!r}")
    return FileResponse(
        c.files.blob_path(meta),
        media_type=meta.media_type,
        filename=meta.name,
        content_disposition_type="inline" if meta.is_inline_image else "attachment",
        headers=_SAFE_HEADERS,
    )
