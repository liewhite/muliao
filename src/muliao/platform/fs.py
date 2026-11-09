"""文件系统的小工具（各领域共用）：原子写入。"""

import os
import stat
import tempfile
from pathlib import Path


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
