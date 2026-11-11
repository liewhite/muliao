"""文件系统的小工具（各领域共用）：路径的身份（real，判断两个路径是不是同一个、在不在某目录下只经它），文本读写
（read_text / write_text / write_atomic），目录内的相对路径解析。

路径的身份只在这里算（tests/test_boundaries.py 禁止别处 `.resolve()` / `os.path.realpath`）：macOS 的 APFS 默认不分
大小写、不分 Unicode 正规化形式，`~/.MULIAO/CONFIG.toml` 与 `~/.muliao/config.toml` 是同一个文件，而 realpath
不纠正大小写，按字符串比就会绕过读写边界与审批。"""

import fcntl
import os
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath

from muliao.errors import InvalidInput

# F_GETPATH 的缓冲区（macOS 的 MAXPATHLEN）
_MAXPATHLEN = 1024


def real(p: Path | str, *, follow: bool = True) -> Path:
    """路径的身份：绝对路径，解析符号链接，每一段换成盘上的真实写法（大小写、正规化形式）。两个路径指同一个文件当且
    仅当 real 相等；「在某目录下」按 real 比。不存在的部分保持原样拼在最近的存在的上级后面。follow 为假时不解析最后
    一段（链接本身的身份）。"""
    # 不用 Path.resolve()：3.12 遇到循环链接抛 RuntimeError；realpath 原样返回，之后的 exists / stat 自然失败
    if follow:
        resolved = os.path.realpath(p)
    else:
        absolute = os.path.abspath(p)
        resolved = os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))
    return Path(_on_disk(resolved))


def _on_disk(path: str) -> str:
    """绝对路径（上级目录里没有符号链接）每一段换成盘上的写法：最深的能打开的目录取内核记的路径（F_GETPATH），
    其下存在的各段按目录列表认（不分大小写与正规化形式）。只有 macOS 需要；别的系统的文件系统按字节区分。"""
    if sys.platform != "darwin":
        return path
    head, tail = path, []
    while True:
        try:
            # O_EVTONLY 不要读权限；O_NOFOLLOW：最后一段是链接时认链接本身（follow=False），交给下面按列表认
            fd = os.open(head, os.O_EVTONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            parent, name = os.path.split(head)
            if parent == head:
                return path
            head = parent
            tail.append(name)
            continue
        try:
            got = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(_MAXPATHLEN))
        finally:
            os.close(fd)
        head = os.fsdecode(got.split(b"\0", 1)[0])
        break
    for i, name in enumerate(reversed(tail)):
        found = _entry(head, name)
        if found is None:
            return os.path.join(head, *reversed(tail[: len(tail) - i]))
        head = os.path.join(head, found)
    return head


def _fold(name: str) -> str:
    return unicodedata.normalize("NFD", name).casefold()


def _entry(directory: str, name: str) -> str | None:
    """directory 里叫 name 的那一项在盘上的写法；不存在为 None。目录列不出（只有 x 没有 r 权限）时打开这一项本身取内核
    记的路径（不要读权限；不当常规路径：硬链接的文件 F_GETPATH 可能给出别的名字）。都不行时报 OSError，不退回给的写法。"""
    path = os.path.join(directory, name)
    if not os.path.lexists(path):
        return None
    try:
        names = os.listdir(directory)
    except OSError:
        flags = os.O_EVTONLY | os.O_NONBLOCK | os.O_CLOEXEC
        flags |= os.O_SYMLINK if os.path.islink(path) else os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            got = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(_MAXPATHLEN))
        finally:
            os.close(fd)
        found = os.path.basename(os.fsdecode(got.split(b"\0", 1)[0]))
        if _fold(found) != _fold(name):
            # 有多个硬链接的文件可能报出另一个链接的名字
            raise OSError(f"cannot tell how {path} is named on disk (got {found!r})") from None
        return found
    if name in names:
        return name
    key = _fold(name)
    return next((n for n in names if _fold(n) == key), name)


def read_text(path: Path) -> str:
    """读 UTF-8 文本，换行符原样保留（CRLF 不转成 LF）。不是 UTF-8 时 InvalidInput（领域错误，交还给调用方）。"""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InvalidInput(
            f"{path} is not UTF-8 text (invalid byte at offset {e.start}); it can only be changed with a command"
        ) from e


def write_text(path: Path, text: str) -> None:
    """按 UTF-8 原样写入（换行符不转换），建好上级目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def resolve_within(root: Path, path: str) -> Path:
    """root 下的相对 posix 路径 path 的身份（real），必须仍在 root 内（按 real 比，经符号链接走出去也不行），否则
    InvalidInput；不查是否存在。界面只读浏览目录（工作目录、skill 附件）、skill_run 找脚本用：只防误走出去，不是安全隔离。"""
    # 文件系统接口遇到 NUL 直接抛 ValueError
    if "\0" in path:
        raise InvalidInput(f"path contains a NUL character: {path!r}")
    rel = PurePosixPath(path)
    if rel.is_absolute():
        raise InvalidInput(f"path must be relative: {path!r}")
    root = real(root)
    target = real(root / rel)
    if not target.is_relative_to(root):
        raise InvalidInput(f"path is outside its directory: {path!r}")
    return target


def write_atomic(path: Path, text: str) -> None:
    """原子写入：先写同目录的临时文件再 rename，读的人看不到写了一半的文件，写到一半失败原文件不动。path 是符号链接时
    写它指向的文件（链接保留）。已有的文件保留原来的权限位，新文件 0644。换行符原样写入。"""
    path = real(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
