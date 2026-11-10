"""一个目录的只读浏览（界面 agent 页的「文件」与 skill 页的文件浏览）：列出目录里的一层、给出一个文件的位置与类型。

路径是相对这个目录的 posix 路径（空串是目录本身），解析符号链接后必须仍在目录内，否则拒绝；指向外面的符号链接
也不列出。这只防止界面误走出去，不是安全隔离（agent 与服务同等可信，docs/architecture.md 第 15 节）。
"""

import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from muliao.errors import InvalidInput, NotFound
from muliao.files.api import INLINE_IMAGE_TYPES
from muliao.platform.fs import real, resolve_within


def media_type_of(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _is_inline(media_type: str) -> bool:
    return media_type in INLINE_IMAGE_TYPES


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


def _target(root: Path, path: str) -> Path:
    """root（已解析）下存在的 path。"""
    target = resolve_within(root, path)
    if not target.exists():
        raise NotFound(f"no such file in the directory: {path!r}")
    return target


def list_dir(root: Path, path: str) -> list[Entry]:
    """目录 root 里 path（相对 root，空为 root 本身）这一层：目录在前，各自按名称排序。指向 root 外的符号链接（打不开）、
    断掉或循环的符号链接等 stat 不了的条目不列出。"""
    root = real(root)
    target = _target(root, path)
    if not target.is_dir():
        raise InvalidInput(f"not a directory: {path!r}")
    entries = []
    for child in target.iterdir():
        if not real(child).is_relative_to(root):
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


def file_in(root: Path, path: str) -> WorkspaceFile:
    """目录 root 里的一个文件（path 相对 root）。"""
    target = _target(real(root), path)
    if not target.is_file():
        raise InvalidInput(f"not a file: {path!r}")
    return WorkspaceFile(target, media_type_of(target.name))


class WorkspaceFiles:
    """agent 工作目录的浏览。"""

    def __init__(self, workspace_of: Callable[[str], Path]):
        self._workspace_of = workspace_of

    def _root(self, agent_id: str) -> Path:
        root = self._workspace_of(agent_id)
        if not root.is_dir():
            raise NotFound(f"agent {agent_id!r} has no workspace")
        return root

    def list(self, agent_id: str, path: str) -> list[Entry]:
        return list_dir(self._root(agent_id), path)

    def file(self, agent_id: str, path: str) -> WorkspaceFile:
        return file_in(self._root(agent_id), path)
