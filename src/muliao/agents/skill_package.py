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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml

from muliao.agents.formats import Draft, Loaded, modified
from muliao.errors import InvalidInput, TooLarge
from muliao.platform.fs import resolve_within

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


def check_files(paths: Iterable[str]) -> None:
    """一个 skill 的全部文件（相对路径，含 SKILL.md）能一起生成到磁盘上：不分大小写、不分 Unicode 规范化形式不重名
    （macOS 默认的文件系统上这样的两个路径是同一个文件），一个路径不能既是文件又是别的文件的上级目录；否则 InvalidInput。
    导入的包与每次写入附件都查（生成目录时撞上会写失败，启动时的全量生成就起不来）。"""
    seen: dict[str, str] = {}
    for path in paths:
        key = unicodedata.normalize("NFC", path).casefold()
        if key in seen:
            hint = f"; {SKILL_FILE} is the instructions" if key == SKILL_FILE.casefold() else ""
            raise InvalidInput(
                f"{path!r} and {seen[key]!r} are the same file (paths are case-insensitive{hint})"
            )
        seen[key] = path
    for key, path in seen.items():
        parent = key.rpartition("/")[0]
        while parent:
            if parent in seen:
                raise InvalidInput(
                    f"{seen[parent]!r} is a file, so it cannot also be a directory of {path!r}"
                )
            parent = parent.rpartition("/")[0]


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
        # 名字的规则由导入方按 skill 的 KindSpec 查（agents.skills）
        name = tops.pop()
        if len(entries) > MAX_PACKAGE_FILES:
            raise TooLarge(f"package has {len(entries)} files, more than {MAX_PACKAGE_FILES}")
        total = sum(info.file_size for info, _ in entries)
        if total > MAX_UNPACKED_BYTES:
            raise TooLarge(f"package unpacks to {total} bytes, more than {MAX_UNPACKED_BYTES}")
        files: dict[str, SkillFile] = {}
        check_files(["/".join(parts[1:]) for _, parts in entries])
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


# ---- SKILL.md 的 frontmatter（导入的包、资源目录里的 skill 都按它） ----


@dataclass(frozen=True)
class Frontmatter:
    description: str
    # 执行前要用户批准的脚本（相对 skill 目录的路径，skill_run 执行前挂起等批准，#530）
    approval: tuple[str, ...]
    post: frontmatter.Post


def parse_frontmatter(text: str, name: str) -> Frontmatter:
    """SKILL.md 的 frontmatter 规则：description 是非空字符串；有 name 时必须与 skill（目录名）同名；有 approval 时是
    相对 skill 目录的脚本路径列表。不合规则时 InvalidInput。"""
    where = f"{name}/{SKILL_FILE}"
    post = loads_skill_md(text, name, where)
    description = post.get("description")
    if not isinstance(description, str):
        raise InvalidInput(f"{where}: frontmatter must contain a string 'description'")
    return Frontmatter(valid_description(description), _approval(post.get("approval", []), where), post)


def _approval(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value):
        raise InvalidInput(
            f"{where}: 'approval' must be a list of script paths relative to the skill directory, "
            f"e.g. [scripts/deploy.sh], got {value!r}"
        )
    for p in value:
        if p.startswith("/"):
            raise InvalidInput(f"{where}: approval path {p!r} must be relative to the skill directory")
    return tuple(p.strip() for p in value)


def approval_scripts(directory: Path, approval: tuple[str, ...]) -> tuple[Path, ...]:
    """frontmatter 的 approval 换成目录里的脚本文件（身份，platform.fs.real，skill_run 按它认要批准的脚本）。哪一项
    不是目录里存在的文件就 InvalidInput：写错的一项若照样加载，那个脚本就静默地不用批准。"""
    where = f"{directory.name}/{SKILL_FILE}"
    found = []
    for p in approval:
        try:
            script = resolve_within(directory, p)
        except InvalidInput as e:
            raise InvalidInput(f"{where}: approval: {e}") from e
        if not script.is_file():
            raise InvalidInput(
                f"{where}: approval path {p!r} is not a file in the skill directory (looked for {script}); "
                "create the script first, or fix the path"
            )
        found.append(script)
    return tuple(found)


def loads_skill_md(text: str, name: str, where: str) -> frontmatter.Post:
    """解析 SKILL.md；frontmatter 里有 name 时必须与 skill 同名。where 是报错里对这份文件的称呼。"""
    try:
        post = frontmatter.loads(text)
    except (yaml.YAMLError, ValueError) as e:
        raise InvalidInput(f"{where}: cannot parse frontmatter: {e}") from e
    fm_name = post.get("name")
    if fm_name is not None and fm_name != name:
        raise InvalidInput(f"{where}: frontmatter name {fm_name!r} does not match {name!r}")
    return post


def valid_description(description: str) -> str:
    description = description.strip()
    if not description:
        raise InvalidInput("skill description must not be empty")
    return description


def skill_md(name: str, description: str, body: str) -> str:
    return frontmatter.dumps(frontmatter.Post(body.strip() + "\n", name=name, description=description)) + "\n"


# ---- 资源目录里的 skill（读树与写入口，agents.kinds 的 KindSpec 登记） ----


@dataclass(frozen=True)
class SkillDir:
    """skill 的内容：描述与执行前要批准的脚本（frontmatter 里列的，读树时已换成目录里存在的文件的身份，
    approval_scripts）。"""

    description: str
    approval: tuple[Path, ...]


def load(path: Path, name: str, _memory_max_chars: int) -> Loaded[SkillDir]:
    """读树：path 是 skill 目录，修改时间取 SKILL.md 的。"""
    md = path / SKILL_FILE
    if not md.is_file():
        raise InvalidInput(f"skill directory has no {SKILL_FILE}")
    fm = parse_frontmatter(md.read_text(encoding="utf-8"), name)
    return Loaded(SkillDir(fm.description, approval_scripts(path, fm.approval)), modified(md))


def vet(draft: Draft) -> str:
    """写入口：SKILL.md 按读树的规则校验；附件要 skill 已有 SKILL.md，否则写出来的是一个读树时加载不了的目录。"""
    if draft.inner == (SKILL_FILE,):
        approval_scripts(draft.item, parse_frontmatter(draft.text, draft.name).approval)
        return draft.text
    if not (draft.item / SKILL_FILE).is_file():
        raise InvalidInput(
            f"{draft.name}/ has no {SKILL_FILE}; write {draft.name}/{SKILL_FILE} first, then its other files"
        )
    return draft.text
