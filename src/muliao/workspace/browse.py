"""工作目录的只读浏览（界面的「文件」页）：列出 agent 工作目录里的一层目录、给出一个文件的位置与类型。

路径是相对工作目录的 posix 路径（空串是工作目录本身），解析符号链接后必须仍在工作目录内，否则拒绝；指向外面的符号链接
也不列出。这只防止界面误走出去，不是安全隔离（agent 与服务同等可信，docs/architecture.md 第 15 节）。
"""

import mimetypes
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from muliao.errors import InvalidInput, NotFound
from muliao.files.api import INLINE_IMAGE_TYPES


def media_type_of(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _is_inline(media_type: str) -> bool:
    return media_type in INLINE_IMAGE_TYPES


def _real(p: Path) -> Path:
    # 不用 Path.resolve()：3.12 遇到循环链接抛 RuntimeError；realpath 原样返回，之后的 exists / stat 自然失败
    return Path(os.path.realpath(p))


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    # 目录为 None
    size: int | None
    modified: datetime
    # 能内联显示的图片（files.INLINE_IMAGE_TYPES），与读文件时的响应一致
    inline: bool


@dataclass(frozen=True)
class WorkspaceFile:
    path: Path
    media_type: str

    @property
    def inline(self) -> bool:
        return _is_inline(self.media_type)


class WorkspaceFiles:
    def __init__(self, workspace_of: Callable[[str], Path]):
        self._workspace_of = workspace_of

    def _resolve(self, agent_id: str, path: str) -> tuple[Path, Path]:
        """(工作目录, 目标)，都已解析符号链接。"""
        # 文件系统接口遇到 NUL 直接抛 ValueError
        if "\0" in path:
            raise InvalidInput(f"path contains a NUL character: {path!r}")
        rel = PurePosixPath(path)
        if rel.is_absolute():
            raise InvalidInput(f"path must be relative to the workspace: {path!r}")
        root = _real(self._workspace_of(agent_id))
        if not root.is_dir():
            raise NotFound(f"agent {agent_id!r} has no workspace")
        target = _real(root / rel)
        if not target.is_relative_to(root):
            raise InvalidInput(f"path is outside the workspace: {path!r}")
        if not target.exists():
            raise NotFound(f"no such file in the workspace: {path!r}")
        return root, target

    def list(self, agent_id: str, path: str) -> list[Entry]:
        """目录在前，各自按名称排序。指向工作目录外的符号链接（打不开）、断掉或循环的符号链接等 stat 不了的条目不列出。"""
        root, target = self._resolve(agent_id, path)
        if not target.is_dir():
            raise InvalidInput(f"not a directory: {path!r}")
        entries = []
        for child in target.iterdir():
            if not _real(child).is_relative_to(root):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            is_dir = child.is_dir()
            entries.append(
                Entry(
                    name=child.name,
                    is_dir=is_dir,
                    size=None if is_dir else st.st_size,
                    modified=datetime.fromtimestamp(st.st_mtime, UTC),
                    inline=not is_dir and _is_inline(media_type_of(child.name)),
                )
            )
        return sorted(entries, key=lambda e: (not e.is_dir, e.name))

    def file(self, agent_id: str, path: str) -> WorkspaceFile:
        _, target = self._resolve(agent_id, path)
        if not target.is_file():
            raise InvalidInput(f"not a file: {path!r}")
        return WorkspaceFile(target, media_type_of(target.name))
