"""标准 .skill 文件：Anthropic Agent Skills 的打包格式（anthropics/skills 仓库 skill-creator/scripts/package_skill.py）。

一个 zip 包，里面唯一的顶层目录与 skill 同名，目录里是 `SKILL.md`（YAML frontmatter：name、description）和附件
（脚本、参考文档……，可在子目录里）。这里只管格式：打包、解包与校验，纯函数；frontmatter 的规则与写入在 SkillStore。

skill 的附件规则也在这里（is_attachment、MAX_FILE_BYTES）：解包、从目录收附件（SkillStore.write 的 attachments）都只收
会存进 skill 的文件（按后缀白名单、不超过上限、UTF-8 文本），其余的不写入、在结果里逐个列出原因：二进制文件不存，
skill 里应写明来源由安装脚本获取。包的结构不对（路径越界、符号链接、不止一个顶层目录、名字不合法、超出总大小与
文件数）整个拒绝。
"""

from __future__ import annotations

import io
import stat
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from muliao.agents.kinds import StoredKind, label
from muliao.agents.naming import validate_slug
from muliao.errors import InvalidInput, TooLarge

SKILL_FILE = "SKILL.md"
# skill 的附件（脚本、配置、参考文档，可在子目录里）：按后缀白名单，UTF-8 文本，单个不超过 MAX_FILE_BYTES。二进制文件
# 不存（skill 里写明版本与来源，由安装脚本获取）
ATTACHMENT_SUFFIXES = (
    "md", "txt", "py", "sh", "bash", "js", "mjs", "cjs", "ts", "json", "toml", "yaml", "yml",
    "sql", "ini", "cfg", "conf", "csv", "html", "css", "jq", "xml", "proto", "tf",
    "go", "mod", "sum", "rs", "java", "kt", "kts", "gradle", "swift", "c", "h", "cc", "cpp", "hpp", "rb", "lua",
    "tsx", "jsx", "vue", "zsh", "ps1",
)  # fmt: skip
MAX_FILE_BYTES = 256 * 1024
# 即使后缀在白名单里也不是附件的：隐藏文件与目录（.env、.venv、.cache……）、依赖与虚拟环境目录
_HIDDEN = "."
NOT_ATTACHMENT_DIRS = ("node_modules", "venv", "site-packages", "__pycache__")


def is_attachment(rel: str) -> bool:
    """skill 目录里的这个相对路径（如 `scripts/run.py`、`SKILL.md`）是不是会存进 skill 的文件类型。"""
    parts = rel.split("/")
    *dirs, name = parts
    if any(p.startswith(_HIDDEN) for p in parts) or any(d in NOT_ATTACHMENT_DIRS for d in dirs):
        return False
    stem, dot, suffix = name.rpartition(".")
    return bool(dot) and bool(stem) and suffix in ATTACHMENT_SUFFIXES


@dataclass(frozen=True)
class SkillFile:
    """skill 里的一个文件（SKILL.md 或附件）。"""

    content: str
    executable: bool = False


def accept(rel: str, size: int, read: Callable[[], bytes], skipped: list[Skipped]) -> SkillFile | None:
    """一个候选文件按附件规则收下（返回内容）或跳过（记进 skipped）。size 先于读取判断。"""
    if not is_attachment(rel):
        skipped.append(Skipped(rel, "not an attachment file type (text files by suffix only)"))
        return None
    if size > MAX_FILE_BYTES:
        skipped.append(Skipped(rel, f"larger than {MAX_FILE_BYTES} bytes"))
        return None
    try:
        return SkillFile(read().decode("utf-8"))
    except UnicodeDecodeError:
        skipped.append(Skipped(rel, "not UTF-8 text"))
        return None


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
    # 相对 skill 目录的路径 → 内容，含 SKILL.md
    files: dict[str, SkillFile]
    skipped: list[Skipped]


def pack(name: str, files: Mapping[str, SkillFile]) -> bytes:
    """files：相对 skill 目录的路径 → 内容，必须含 SKILL.md。可执行位写进条目的权限（同 package_skill.py 的
    zipfile.write），导入时还原。"""
    if SKILL_FILE not in files:
        raise InvalidInput(f"skill {name!r} has no {SKILL_FILE}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{name}/{rel}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | (0o755 if files[rel].executable else 0o644)) << 16
            archive.writestr(info, files[rel].content.encode())
    return buffer.getvalue()


def path_parts(path: str, what: str) -> list[str]:
    """skill 目录里的一个相对路径（zip 条目、附件名）拆成各段；绝对路径、`..`、`.`、空段、反斜杠（Windows 分隔符）
    都是越界或歧义，拒绝。what 是报错里对它的称呼。"""
    if path.startswith("/") or "\\" in path:
        raise InvalidInput(f"{what} {path!r} is absolute or uses backslashes")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidInput(f"{what} {path!r} has an empty, '.' or '..' segment")
    return parts


def check_attachment_path(rel: str) -> None:
    """rel 能作为附件存进 skill：skill 目录内的相对路径、附件的文件类型，且不是 SKILL.md；否则 InvalidInput。"""
    path_parts(rel, "attachment path")
    if rel == SKILL_FILE or not is_attachment(rel):
        raise InvalidInput(
            f"{rel!r} cannot be an attachment of a skill (text files ending in "
            f"{', '.join('.' + s for s in ATTACHMENT_SUFFIXES)}, not hidden or inside "
            f"{', '.join(NOT_ATTACHMENT_DIRS)})"
        )


def _parts(filename: str) -> list[str]:
    """zip 里的一个路径（目录条目以 `/` 结尾）拆成各段；越界或歧义的整个包拒绝。"""
    return path_parts(filename.removesuffix("/"), "package path")


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
        name = validate_slug(label(StoredKind.SKILL), tops.pop())
        if len(entries) > MAX_PACKAGE_FILES:
            raise TooLarge(f"package has {len(entries)} files, more than {MAX_PACKAGE_FILES}")
        total = sum(info.file_size for info, _ in entries)
        if total > MAX_UNPACKED_BYTES:
            raise TooLarge(f"package unpacks to {total} bytes, more than {MAX_UNPACKED_BYTES}")
        files: dict[str, SkillFile] = {}
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

            def read(info: zipfile.ZipInfo = info) -> bytes:
                try:
                    return archive.read(info)
                except (zipfile.BadZipFile, zlib.error, NotImplementedError) as e:
                    raise InvalidInput(f"cannot read {info.filename!r} from the package: {e}") from e

            accepted = accept(rel, info.file_size, read, skipped)
            if accepted is not None:
                executable = bool((info.external_attr >> 16) & 0o111)
                files[rel] = SkillFile(accepted.content, executable)
    if SKILL_FILE not in files:
        why = next((f": {s.reason}" for s in skipped if s.path == SKILL_FILE), "")
        raise InvalidInput(f"package has no usable {name}/{SKILL_FILE}{why}")
    return Package(name, files, skipped)
