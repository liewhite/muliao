"""文件系统的小工具（各领域共用）：原子写入，目录内的相对路径解析。"""

import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

from muliao.errors import InvalidInput


def real(p: Path) -> Path:
    """解析符号链接后的路径。"""
    # 不用 Path.resolve()：3.12 遇到循环链接抛 RuntimeError；realpath 原样返回，之后的 exists / stat 自然失败
    return Path(os.path.realpath(p))


def resolve_within(root: Path, path: str) -> Path:
    """root（已解析）下的相对 posix 路径 path，解析符号链接后必须仍在 root 内，否则 InvalidInput；不查是否存在。
    界面只读浏览目录（工作目录、skill 附件）用：只防误走出去，不是安全隔离。"""
    # 文件系统接口遇到 NUL 直接抛 ValueError
    if "\0" in path:
        raise InvalidInput(f"path contains a NUL character: {path!r}")
    rel = PurePosixPath(path)
    if rel.is_absolute():
        raise InvalidInput(f"path must be relative: {path!r}")
    target = real(root / rel)
    if not target.is_relative_to(root):
        raise InvalidInput(f"path is outside its directory: {path!r}")
    return target


def write_atomic(path: Path, text: str, *, executable: bool | None = None) -> None:
    """原子写入：先写同目录的临时文件再 rename，读的人看不到写了一半的文件，写到一半失败原文件不动。path 是符号链接时
    写它指向的文件（链接保留）。已有的文件保留原来的权限位，executable 给出时只改可执行位；新文件 0644（executable 时
    0755）。"""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else 0o644
    if executable is True:
        mode |= 0o111
    elif executable is False:
        mode &= ~0o111
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
