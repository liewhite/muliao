"""标准 .skill 文件：Anthropic Agent Skills 的打包格式（anthropics/skills 仓库 skill-creator/scripts/package_skill.py）。

一个 zip 包，里面唯一的顶层目录与 skill 同名，目录里是 `SKILL.md`（YAML frontmatter：name、description）和附件
（脚本、参考文档……，可在子目录里）。这里只管格式：打包、解包与校验，纯函数；frontmatter 的规则与写入在 SkillStore。

解包只收会进版本管理的文件（resources.is_attachment 的白名单、MAX_EXTERNAL_FILE_BYTES 上限、UTF-8 文本），其余的
不写入、在结果里逐个列出原因：二进制文件不进历史，skill 里应写明来源由安装脚本获取。包的结构不对（路径越界、
符号链接、不止一个顶层目录、名字不合法、超出总大小与文件数）整个拒绝。
"""

from __future__ import annotations

import io
import stat
import unicodedata
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass

from muliao.agents.naming import validate_slug
from muliao.agents.resources import MAX_EXTERNAL_FILE_BYTES, VersionedFile, is_attachment
from muliao.errors import InvalidInput, TooLarge

SKILL_FILE = "SKILL.md"
# 上传的包本身的大小上限（HTTP 按它读请求体）
MAX_PACKAGE_BYTES = 10 * 1024 * 1024
# 解开后的文件数与总大小上限（按 zip 目录里声明的大小，读之前判断：防压缩炸弹）
MAX_PACKAGE_FILES = 1000
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
# macOS 的「压缩」顺手放进去的元数据目录：不是 skill 的一部分，不算顶层目录
_MACOS_METADATA = "__MACOSX"


@dataclass(frozen=True)
class Skipped:
    """包里没有导入的一个文件。"""

    path: str
    reason: str


@dataclass(frozen=True)
class Package:
    name: str
    # 相对 skill 目录的路径 → 文本内容，含 SKILL.md
    files: dict[str, str]
    skipped: list[Skipped]


def pack(name: str, files: Mapping[str, VersionedFile]) -> bytes:
    """files：相对 skill 目录的路径 → 内容，必须含 SKILL.md。可执行位写进条目的权限（同 package_skill.py 的
    zipfile.write）。导入时不还原权限：附件按文本写入，脚本用解释器运行。"""
    if SKILL_FILE not in files:
        raise InvalidInput(f"skill {name!r} has no {SKILL_FILE} in its version history")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{name}/{rel}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (0o755 if files[rel].executable else 0o644)) << 16
            archive.writestr(info, files[rel].data)
    return buffer.getvalue()


def _parts(filename: str) -> list[str]:
    """zip 里的一个路径拆成各段；绝对路径、`..`、反斜杠（Windows 分隔符）都是越界或歧义，整个包拒绝。"""
    if filename.startswith("/") or "\\" in filename:
        raise InvalidInput(f"package path {filename!r} is absolute or uses backslashes")
    parts = filename.removesuffix("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidInput(f"package path {filename!r} has an empty, '.' or '..' segment")
    return parts


def unpack(data: bytes) -> Package:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise InvalidInput(f"not a .skill (zip) file: {e}") from e
    with archive:
        entries: list[tuple[zipfile.ZipInfo, list[str]]] = []
        skipped: list[Skipped] = []
        for info in archive.infolist():
            parts = _parts(info.filename)
            if parts[0] == _MACOS_METADATA:
                if not info.is_dir():
                    skipped.append(Skipped(info.filename, "macOS metadata"))
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise InvalidInput(f"package path {info.filename!r} is a symbolic link")
            if info.flag_bits & 0x1:
                raise InvalidInput(f"package path {info.filename!r} is encrypted")
            if not info.is_dir():
                entries.append((info, parts))
        tops = {parts[0] for _, parts in entries}
        if len(tops) != 1 or any(len(parts) < 2 for _, parts in entries):
            raise InvalidInput(
                f"a .skill package must contain exactly one top-level directory holding all files, found {sorted(tops)}"
            )
        name = validate_slug("skill", tops.pop())
        if len(entries) > MAX_PACKAGE_FILES:
            raise TooLarge(f"package has {len(entries)} files, more than {MAX_PACKAGE_FILES}")
        total = sum(info.file_size for info, _ in entries)
        if total > MAX_UNPACKED_BYTES:
            raise TooLarge(f"package unpacks to {total} bytes, more than {MAX_UNPACKED_BYTES}")
        files: dict[str, str] = {}
        # 按不分大小写、不分 Unicode 规范化形式比较：macOS 默认的文件系统上这样的两个路径是同一个文件
        seen: set[str] = set()
        for info, parts in entries:
            rel = unicodedata.normalize("NFC", "/".join(parts[1:])).casefold()
            if rel in seen:
                raise InvalidInput(
                    f"package contains {info.filename!r} more than once (paths are case-insensitive)"
                )
            seen.add(rel)
        # 一个路径不能既是文件又是别的文件的上级目录
        for rel in seen:
            parent = rel.rpartition("/")[0]
            while parent:
                if parent in seen:
                    raise InvalidInput(f"package path {name}/{parent} is both a file and a directory")
                parent = parent.rpartition("/")[0]
        for info, parts in entries:
            rel = "/".join(parts[1:])
            if not is_attachment(rel):
                skipped.append(Skipped(rel, "not a versioned file type (text attachments by suffix only)"))
                continue
            if info.file_size > MAX_EXTERNAL_FILE_BYTES:
                skipped.append(Skipped(rel, f"larger than {MAX_EXTERNAL_FILE_BYTES} bytes"))
                continue
            try:
                content = archive.read(info)
            except (zipfile.BadZipFile, zlib.error, NotImplementedError) as e:
                raise InvalidInput(f"cannot read {info.filename!r} from the package: {e}") from e
            try:
                files[rel] = content.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(Skipped(rel, "not UTF-8 text"))
    if SKILL_FILE not in files:
        why = next((f": {s.reason}" for s in skipped if s.path == SKILL_FILE), "")
        raise InvalidInput(f"package has no usable {name}/{SKILL_FILE}{why}")
    return Package(name, files, skipped)
