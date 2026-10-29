"""工作目录的只读浏览（界面的「文件」页）：列出 agent 工作目录里的一层目录、给出一个文件的位置。

路径是相对工作目录的 posix 路径（空串是工作目录本身），解析符号链接后必须仍在工作目录内，否则拒绝；
这只防止界面误走出去，不是安全隔离（agent 与服务同等可信，docs/architecture.md 第 15 节）。
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from muliao.errors import InvalidInput, NotFound


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    # 目录为 None
    size: int | None
    modified: datetime


class WorkspaceFiles:
    def __init__(self, workspace_of: Callable[[str], Path]):
        self._workspace_of = workspace_of

    def _resolve(self, agent_id: str, path: str) -> Path:
        rel = PurePosixPath(path)
        if rel.is_absolute():
            raise InvalidInput(f"path must be relative to the workspace: {path!r}")
        root = self._workspace_of(agent_id).resolve()
        if not root.is_dir():
            raise NotFound(f"agent {agent_id!r} has no workspace")
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise InvalidInput(f"path is outside the workspace: {path!r}")
        if not target.exists():
            raise NotFound(f"no such file in the workspace: {path!r}")
        return target

    def list(self, agent_id: str, path: str) -> list[Entry]:
        """目录在前，各自按名称排序。断掉的符号链接等 stat 不了的条目不列出。"""
        target = self._resolve(agent_id, path)
        if not target.is_dir():
            raise InvalidInput(f"not a directory: {path!r}")
        entries = []
        for child in target.iterdir():
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
                )
            )
        return sorted(entries, key=lambda e: (not e.is_dir, e.name))

    def file(self, agent_id: str, path: str) -> Path:
        target = self._resolve(agent_id, path)
        if not target.is_file():
            raise InvalidInput(f"not a file: {path!r}")
        return target
