"""运行时（agent_runtime）在 pydantic-ai 消息上写下的约定：工具交回给用户的文件与图片。新增约定先在这里登记，解析也只在
这里写一份。会话领域自己的约定（停止、事件信件、运行上下文、子会话等）在 sessions.conventions。

| 约定 | 记在哪里 | 谁写 | 谁读 |
|---|---|---|---|
| `TOOL_ATTACHMENTS`：工具挂给用户的文件 | ToolReturnPart.metadata | share_file、ask_agent（sessions.answers） | 会话条目、实时事件、转交给调用方 |
| 工具交回的图片（`result_images`） | ToolReturnPart.content 列表里的 ImageUrl（文件引用） | attachments.ToolImagesToFiles（如 MCP 截图） | 模型（AttachmentsForModel 展开）、会话条目、实时事件；不转交调用方 |

metadata 的键带 `muliao_` 前缀：metadata 是 pydantic-ai 开放给所有工具（含 MCP 包装）的字段，通用的键名可能撞上形状不同的
数据。
"""

from collections.abc import Sequence
from typing import TypedDict

from pydantic import TypeAdapter
from pydantic_ai.messages import (
    DocumentUrl,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    RetryPromptPart,
    ToolReturnPart,
)

from muliao.files.api import FileMeta, parse_file_url


class Attachment(TypedDict):
    # files.file_url：界面直接用它显示与下载
    url: str
    name: str
    media_type: str
    # 能内联显示的图片（files.INLINE_IMAGE_TYPES）；其余是下载链接
    inline: bool


# ToolReturn(metadata={TOOL_ATTACHMENTS: [Attachment, ...]})：文件一定会显示给用户，不依赖模型在回答里转写引用
TOOL_ATTACHMENTS = "muliao_attachments"
_ATTACHMENTS = TypeAdapter(list[Attachment])


def attachment_of(meta: FileMeta) -> Attachment:
    return Attachment(url=meta.url, name=meta.name, media_type=meta.media_type, inline=meta.is_inline_image)


def tool_attachments(part: ToolReturnPart | RetryPromptPart) -> list[Attachment]:
    """工具结果挂上的文件（历史条目与实时事件共用这一处解析）。其他工具或 MCP 的 metadata 不带这个键时为空。"""
    if not isinstance(part, ToolReturnPart) or not isinstance(part.metadata, dict):
        return []
    if TOOL_ATTACHMENTS not in part.metadata:
        return []
    attachments = _ATTACHMENTS.validate_python(part.metadata[TOOL_ATTACHMENTS])
    # 前端把 url 直接放进 src/href：只能是本站文件存储的引用
    for a in attachments:
        parse_file_url(a["url"])
    return attachments


def attachment_of_ref(ref: ImageUrl | DocumentUrl) -> Attachment:
    """消息里一个文件引用（存储形态）的展示形式：图片内联，其余是下载链接。"""
    return Attachment(
        url=ref.url,
        name=parse_file_url(ref.url)[1],
        media_type=ref.media_type,
        inline=isinstance(ref, ImageUrl),
    )


def result_image_refs(part: object) -> list[ImageUrl]:
    """工具交回、已存进文件存储的图片引用（attachments.ToolImagesToFiles 写下：有图片的结果总是内容列表）。"""
    if not isinstance(part, ToolReturnPart) or not isinstance(part.content, list):
        return []
    return [c for c in part.content if isinstance(c, ImageUrl)]


def result_images(part: ToolReturnPart | RetryPromptPart) -> list[Attachment]:
    """工具交回的图片的展示形式。它们是给模型看的结果，会话里显示在这次调用的过程里（Item.images）；不是挂给用户的
    文件（TOOL_ATTACHMENTS），所以不进 shared_attachments、不转交调用方。"""
    return [attachment_of_ref(r) for r in result_image_refs(part)]


def shared_attachments(messages: Sequence[ModelMessage]) -> list[Attachment]:
    """这些消息里所有工具结果挂上的文件，按出现顺序。函数工具的结果只出现在 ModelRequest 里。"""
    return [
        a
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
        for a in tool_attachments(p)
    ]
