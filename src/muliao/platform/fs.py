"""文件系统的小工具（各领域共用）：原子写入。"""

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, text: str, *, executable: bool | None = None) -> None:
    """原子写入：先写同目录的临时文件再 rename，读的人看不到写了一半的文件，写到一半失败原文件不动。path 是符号链接时
    写它指向的文件（链接保留）。executable 为 None 时保留原文件的可执行位（新文件不可执行）。"""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if executable is None:
        executable = path.is_file() and os.access(path, os.X_OK)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o755 if executable else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
