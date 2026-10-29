"""对话里出现的文件（用户发送的图片与附件、agent 用 share_file 发出的文件、工具交回的图片）：内容不可变，按 id 引用。

做法与主流 IM 的媒体仓库一致（Matrix media repository、Slack files）：先上传得到 id，消息里只存引用；
文件写入后不再改变，聊天记录里的引用永远指向同一份内容。文件与消息相互独立，删除会话不删除文件。

- 元数据（名称、类型、大小、哈希）在 `files` 表，是唯一的索引；内容按 sha256 存在 `data/files/<前两位>/<sha256>`，
  相同内容只存一份。
- 引用的唯一形式是 `file_url`：`/api/files/<id>/<name>`。界面直接用它显示与下载，消息里的
  ImageUrl / DocumentUrl 也存它（见 agent_runtime.attachments）。

用最终正确性换简化：内容与元数据不在一个事务里。先把内容写进临时文件、改名到最终位置，再插入元数据行。
- 失败可见：任何一步失败都向上报错，调用方看到的是上传失败，没有返回 id 就没有任何引用。
- 可收敛：进程中途被杀留下的临时文件由启动时的 `recover` 清掉；已改名、没有元数据行的内容不会被任何
  引用访问到（只能经元数据按 id 找到），同样内容再次上传时直接复用。
- 代价可控：孤儿内容只占空间。
"""

import asyncio
import hashlib
import logging
import os
import unicodedata
import uuid
from collections.abc import AsyncIterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import CorruptState, InvalidInput, NotFound, TooLarge
from muliao.files.tables import StoredFile
from muliao.platform.db import Database
from muliao.platform.ids import new_id

log = logging.getLogger(__name__)

FILES_PATH = "/api/files"
# 浏览器可以直接显示、模型也普遍能看的图片类型：只有这些按 inline 返回，并作为图片交给模型。
# SVG 不在其中：它能带脚本
INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
# 与界面同源、带着登录 cookie 下载 agent 写的内容时的响应头（对话里的文件、工作目录的文件）：不让浏览器猜类型、在沙箱里打开。
# 只有 INLINE_IMAGE_TYPES 按 inline 返回，其余作为附件（docs/architecture.md 第 15 节）
UNTRUSTED_CONTENT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
}
# 文件系统（APFS、ext4）对单个文件名的限制是 255 个 UTF-8 字节，名称会原样成为工作目录里的文件名
_NAME_MAX_BYTES = 255
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileMeta:
    id: str
    name: str
    media_type: str
    size: int
    sha256: str
    created_at: datetime

    @property
    def url(self) -> str:
        return file_url(self.id, self.name)

    @property
    def is_inline_image(self) -> bool:
        return self.media_type in INLINE_IMAGE_TYPES


def file_url(file_id: str, name: str) -> str:
    return f"{FILES_PATH}/{file_id}/{quote(name, safe='')}"


def parse_file_url(url: str) -> tuple[str, str]:
    """file_url 的逆：取出 id 与名称。不是 file_url 生成的地址说明数据与代码不一致，直接报错。"""
    prefix = f"{FILES_PATH}/"
    file_id, sep, quoted = url.removeprefix(prefix).partition("/")
    if not url.startswith(prefix) or not sep or not file_id or not quoted:
        raise RuntimeError(f"{url!r} is not a file reference produced by file_url")
    return file_id, unquote(quoted)


def check_name(name: str) -> str:
    """文件名会成为 URL 的一段和 agent 工作目录里的文件名：只接受单个路径段，不做修正。"""
    size = len(name.encode())
    if not name or size > _NAME_MAX_BYTES:
        raise InvalidInput(f"file name must be 1..{_NAME_MAX_BYTES} bytes in UTF-8, got {size}")
    if name in (".", "..") or any(c in "/\\" or unicodedata.category(c) == "Cc" for c in name):
        raise InvalidInput(f"file name {name!r} must be a single path segment without control characters")
    return name


def check_media_type(media_type: str) -> str:
    """只存 type/subtype：参数（如 charset）不影响怎么显示和交给模型，下载一律按原始字节。"""
    essence = media_type.partition(";")[0].strip()
    kind, sep, subtype = essence.partition("/")
    if not sep or not kind or not subtype or any(c.isspace() for c in essence):
        raise InvalidInput(f"media type {media_type!r} must be 'type/subtype'")
    return essence.lower()


async def load_metas(s: AsyncSession, file_ids: Sequence[str]) -> list[FileMeta]:
    """按给出的顺序返回元数据；任何一个不存在都报 NotFound。files 表只经过这里读。"""
    rows = {r.id: r for r in await s.scalars(select(StoredFile).where(StoredFile.id.in_(file_ids)))}
    missing = [i for i in file_ids if i not in rows]
    if missing:
        raise NotFound(f"files {missing} not found")
    return [_meta(rows[i]) for i in file_ids]


def _meta(row: StoredFile) -> FileMeta:
    return FileMeta(
        id=row.id,
        name=row.name,
        media_type=row.media_type,
        size=row.size,
        sha256=row.sha256,
        created_at=row.created_at,
    )


class FileStore:
    def __init__(self, db: Database, root: Path, max_bytes: int):
        self._db = db
        self._root = root
        self._tmp = root / "tmp"
        self.max_bytes = max_bytes

    def blob_path(self, meta: FileMeta) -> Path:
        return self._root / meta.sha256[:2] / meta.sha256

    def recover(self) -> None:
        """启动时（还没有上传在进行）清掉上次进程中途被杀留下的临时文件。"""
        if not self._tmp.exists():
            return
        leftovers = list(self._tmp.iterdir())
        for p in leftovers:
            p.unlink()
        if leftovers:
            log.warning("removed unfinished uploads", extra={"count": len(leftovers)})

    async def put(self, name: str, media_type: str, chunks: AsyncIterable[bytes]) -> FileMeta:
        """边读边写边算哈希，超过上限立即停止读取。"""
        name = check_name(name)
        media_type = check_media_type(media_type)
        self._tmp.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp / uuid.uuid4().hex
        digest = hashlib.sha256()
        size = 0
        try:
            # 分块写本地磁盘很快，不为每块切线程
            with tmp.open("wb") as f:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TooLarge(f"file exceeds {self.max_bytes} bytes")
                    digest.update(chunk)
                    f.write(chunk)
            sha = digest.hexdigest()
            await asyncio.to_thread(self._commit_blob, tmp, self._root / sha[:2] / sha)
        finally:
            tmp.unlink(missing_ok=True)
        async with self._db.transaction() as s:
            row = StoredFile(id=new_id(), sha256=sha, name=name, media_type=media_type, size=size)
            s.add(row)
            await s.flush()
            meta = _meta(row)
        log.info("file stored", extra={"file_id": meta.id, "media_type": media_type, "size": size})
        return meta

    @staticmethod
    def _commit_blob(tmp: Path, blob: Path) -> None:
        """已有的内容不覆盖（它可能已被引用，原件不可变）；新内容先落盘再改名，再让目录项落盘，
        掉电后不会出现元数据行指向空文件或截断内容的情况。"""
        if blob.exists():
            return
        blob.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, blob)
        fd = os.open(blob.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    async def put_path(self, name: str, media_type: str, path: Path) -> FileMeta:
        async def chunks() -> AsyncIterable[bytes]:
            with path.open("rb") as f:
                while chunk := f.read(_CHUNK_BYTES):
                    yield chunk

        return await self.put(name, media_type, chunks())

    async def get(self, file_id: str) -> FileMeta:
        return (await self.get_many([file_id]))[0]

    async def get_many(self, file_ids: Sequence[str]) -> list[FileMeta]:
        """调用方给出的 id（可能有误）：不存在报 NotFound。"""
        async with self._db.transaction() as s:
            return await load_metas(s, file_ids)

    async def referenced(self, file_ids: Sequence[str]) -> list[FileMeta]:
        """系统自己存下的引用（task payload、消息里的附件）：文件从不删除，找不到是不变量被破坏，不是输入错误。"""
        try:
            return await self.get_many(file_ids)
        except NotFound as e:
            raise CorruptState(f"stored references point to missing files: {e}") from e

    def read(self, meta: FileMeta) -> bytes:
        return self.blob_path(meta).read_bytes()
