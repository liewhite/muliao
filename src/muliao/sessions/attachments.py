"""消息里的附件有两种形态，只在发给模型的那一刻转换：

- 存储形态（数据库、界面、重放）：`UserPromptPart.content` 里的 ImageUrl / DocumentUrl，url 是 `files.file_url`，
  只是引用。界面直接用它显示图片和下载链接。
- 模型形态：每次模型请求前由 `AttachmentsForModel` 展开成一段说明（名称、类型、大小、在工作目录里的路径），
  能内联的图片再附上内容。展开只改这一次请求，不改持久化的历史，所以数据库里不会出现文件内容的副本。

附件同时复制进 agent 工作目录的 `uploads/<id>/<名称>`，供文件工具与 shell 处理（与 ChatGPT 把上传文件放进
沙箱目录的做法一致）。那是派生副本，agent 可以随意修改；不可变的原件只在 FileStore 里。
"""

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    BinaryContent,
    DocumentUrl,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext

from muliao.agent_runtime.scope import RunScope
from muliao.files.store import FileMeta, FileStore, parse_file_url

UPLOADS_DIR = "uploads"


def references(files: Sequence[FileMeta]) -> list[UserContent]:
    """附件的存储形态。"""
    return [
        ImageUrl(url=f.url, media_type=f.media_type)
        if f.is_inline_image
        else DocumentUrl(url=f.url, media_type=f.media_type)
        for f in files
    ]


def workspace_path(meta: FileMeta) -> PurePosixPath:
    """附件在 agent 工作目录里的相对路径：按 id 分目录，同名的不同文件互不覆盖，名称保持原样。"""
    return PurePosixPath(UPLOADS_DIR, meta.id, meta.name)


async def copy_to_workspace(store: FileStore, files: Sequence[FileMeta], workspace: Path) -> None:
    """每次执行这一轮都重新复制：重试时覆盖成原件，结果总是一样。"""

    def copy() -> None:
        for meta in files:
            target = workspace / workspace_path(meta)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(store.blob_path(meta), target)

    await asyncio.to_thread(copy)


def describe(meta: FileMeta) -> str:
    return (
        f"[Attached file {meta.name!r} ({meta.media_type}, {meta.size} bytes), "
        f"copied into your workspace at {workspace_path(meta)}]"
    )


@dataclass
class AttachmentsForModel(AbstractCapability[RunScope]):
    store: FileStore

    async def _expand(self, item: UserContent, metas: dict[str, FileMeta]) -> list[UserContent]:
        if not isinstance(item, ImageUrl | DocumentUrl):
            return [item]
        meta = metas[parse_file_url(item.url)[0]]
        if not meta.is_inline_image:
            return [describe(meta)]
        data = await asyncio.to_thread(self.store.read, meta)
        return [describe(meta), BinaryContent(data, media_type=meta.media_type)]

    async def _expand_message(self, message: ModelMessage, metas: dict[str, FileMeta]) -> ModelMessage:
        if not isinstance(message, ModelRequest):
            return message
        parts = []
        for part in message.parts:
            if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
                content = [x for item in part.content for x in await self._expand(item, metas)]
                part = replace(part, content=content)
            parts.append(part)
        return replace(message, parts=parts)

    async def before_model_request(
        self, ctx: RunContext[RunScope], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        # 换掉的是这次请求的消息列表本身，持久化的历史（ctx.messages）保持存储形态
        ids = [
            parse_file_url(item.url)[0]
            for m in request_context.messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart) and not isinstance(p.content, str)
            for item in p.content
            if isinstance(item, ImageUrl | DocumentUrl)
        ]
        metas = {f.id: f for f in await self.store.referenced(list(dict.fromkeys(ids)))}
        messages = [await self._expand_message(m, metas) for m in request_context.messages]
        return replace(request_context, messages=messages)

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None
