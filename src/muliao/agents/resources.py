"""agent 能改的文本资源（记忆、skill、MCP 配置）存成文件，用 git 管理版本。

布局（resources 根目录就是一个 git 仓库）：

    public/                 公共资源：MEMORY.md（全局记忆）、skills/<name>/SKILL.md、mcp/<name>.json
    agents/<agent id>/      agent 私有资源：MEMORY.md、skills/<name>/SKILL.md、mcp/<name>.json

每次修改写文件后提交一次，作者是 "user" 或做出修改的 agent 的 id。历史与回滚都来自 git：
回滚就是把旧版本的内容写回并作为新的一次提交，历史只增不改。

写入是原子的：读取—变换—写入—提交在同一把锁里（update），任何一步失败都把涉及的路径在文件系统上还原成写入前
的样子（不依赖 git），所以「报错」等于「没有生效」，调用方可以放心重试；还原本身失败时记 error 并一起抛出。

资源文件（RESOURCE_FILES，唯一定义）是上面布局里列出的那些，加上 skill 目录里的文本附件（脚本、配置、参考文档，
按后缀白名单，可在子目录里），版本管理只管它们。附件没有专门的工具：agent 用文件工具或 shell 写，由下面的 reconcile 收进历史。

仓库也会被绕过本模块写入：agent 有 shell 与文件工具，能直接改资源文件（read_skill 会给出 skill 目录）；进程在写入
中途被杀也会留下写完未提交的变化。两者用同一个机制收敛：每次写入前、以及启动时（recover），把资源文件上不经本模块的
变化先单独提交为一次 reconcile（作者记为系统，表示「来源不明的外部写入」）。这样外部写入最迟在下一次任何资源写入时
进入历史，也不会被并进下一个作者的提交里。其余的东西（二进制与不在白名单里的文件、隐藏文件、虚拟环境与
依赖目录、嵌套的 git 仓库、自己的临时文件、超过 MAX_EXTERNAL_FILE_BYTES 的外部写入）不进历史：历史只增不改，
进去就删不掉；一个 agent 目录里的杂物（包括暂存不了的坏文件）也不能让所有资源写入失败。
secret 不在这里：它们加密存在数据库的 vault 里，不需要版本管理。
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import shutil
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

import anyio

from muliao.agents.ids import AGENT_ID
from muliao.agents.ownership import scope_of
from muliao.errors import CorruptState, InvalidInput, NotFound

log = logging.getLogger(__name__)

# 为 owner 写资源之前的检查（实现见 agents.active.ActiveOwners）；owner 为 None（公共资源）时不查
OwnerCheck = Callable[[str | None], Awaitable[None]]
# 一个目录里在版本管理中的文件（暂存区里的路径）：apply_planned 交给 plan 用
TrackedFiles = Callable[[Path], Awaitable[list[Path]]]
# git 里普通文件的两种模式
_REGULAR, _EXECUTABLE = "100644", "100755"


@dataclass(frozen=True)
class VersionedFile:
    data: bytes
    # git 里记的可执行位（100755）
    executable: bool


USER = "user"
# 系统自己做的提交（初始化、启动时的收敛）
SYSTEM = "muliao"
RECONCILE_MESSAGE = "reconcile changes made outside the resource tools"
_PUBLIC = "public"
_AGENTS = "agents"
_FIELD = "\x1f"
_REV = re.compile(r"^[0-9a-f]{40}$")
_TMP = ".tmp"
# 各 store 写的资源文件（相对仓库根目录的 glob，`*` 不跨目录）：见模块说明的布局。
# 文件名由各 store 拥有（MEMORY_FILE、SKILL_FILE……），这里是副本，由 test_every_file_the_stores_write_is_in_the_resource_layout 守护
STORE_FILES = (
    "public/MEMORY.md",
    "public/skills/*/SKILL.md",
    "public/mcp/*.json",
    "agents/*/MEMORY.md",
    "agents/*/skills/*/SKILL.md",
    "agents/*/mcp/*.json",
)
# skill 目录里的文本附件（脚本、配置、参考文档，可在子目录里）：没有专门的工具，agent 用文件工具或 shell 写，
# 由 reconcile 收进历史。按后缀白名单：二进制文件不进 git（skill 里写明版本与来源，由安装脚本获取）
ATTACHMENT_SUFFIXES = (
    "md", "txt", "py", "sh", "bash", "js", "mjs", "cjs", "ts", "json", "toml", "yaml", "yml",
    "sql", "ini", "cfg", "csv", "html", "css", "jq",
)  # fmt: skip
_SKILL_DIRS = ("public/skills/*", "agents/*/skills/*")
# `/**/` 匹配零到多层目录
ATTACHMENT_FILES = tuple(f"{d}/**/*.{s}" for d in _SKILL_DIRS for s in ATTACHMENT_SUFFIXES)
# 资源文件 = store 写的 + 附件。reconcile 只处理它们
RESOURCE_FILES = STORE_FILES + ATTACHMENT_FILES
# skill 目录里即使后缀在白名单里也不是附件的：隐藏文件与目录（.env、.venv、.cache……）、依赖与虚拟环境目录。
# 只作用在 skill 目录里面（git 的 exclude 对整组 pathspec 生效，所以带上 skill 目录前缀），不影响 store 写的文件
_HIDDEN = "."
_NOT_ATTACHMENT_DIRS = ("node_modules", "venv", "site-packages", "__pycache__")
_NOT_ATTACHMENTS = (f"{_HIDDEN}*", f"{_HIDDEN}*/**", *(f"{d}/**" for d in _NOT_ATTACHMENT_DIRS))
EXCLUDED_FILES = tuple(f"{d}/**/{p}" for d in _SKILL_DIRS for p in _NOT_ATTACHMENTS)
# 外部写入的单个文件上限：超过的不收进历史（历史只增不改，进去就删不掉），记 warning。已在历史里的文件变得超限：
# 历史停在最后一个未超限的版本，工作区保持未提交，缩回上限以内再照常提交。尽力而为：查大小与暂存之间
# 正在被 shell 写的文件可能已变大
MAX_EXTERNAL_FILE_BYTES = 256 * 1024


def is_attachment(rel: str) -> bool:
    """skill 目录里的这个相对路径（如 `scripts/run.py`、`SKILL.md`）是不是会进版本管理的文件：给还没写到磁盘上的文件
    （导入的 .skill 包）预先判断。与上面交给 git 的 ATTACHMENT_FILES / EXCLUDED_FILES 是同一条规则的两种写法，
    由 test_is_attachment_agrees_with_git 对照 git 的判断守护。"""
    parts = rel.split("/")
    *dirs, name = parts
    if any(p.startswith(_HIDDEN) for p in parts) or any(d in _NOT_ATTACHMENT_DIRS for d in dirs):
        return False
    stem, dot, suffix = name.rpartition(".")
    return bool(dot) and bool(stem) and suffix in ATTACHMENT_SUFFIXES


def _literal(rel: str) -> str:
    """按字面匹配一个路径：git 默认把路径当通配模式，叫 `*.py` 的文件会把排除的、超限的文件一起带进历史。
    不用全局的 --literal-pathspecs：那会让 RESOURCE_PATHSPEC 里的魔法失效。"""
    return f":(literal){rel}"


def _under_common(rel: Sequence[str]) -> tuple[str, ...]:
    """只接受命令行路径的 git 命令（ls-files、diff）用的 pathspec：这些路径的公共上级，结果再用 _covered 取交集。
    没有公共上级时是整个仓库（只用于列出，不用于 add/commit）。"""
    common = posixpath.commonpath(rel)
    return ("--", _literal(common)) if common else ("--",)


def _covered(rel: Sequence[str], files: Iterable[str]) -> set[str]:
    """rel 里被 files 覆盖的：本身在 files 里，或（目录）它下面有文件在。"""
    present = set()
    for f in files:
        if f:
            present.add(f)
            present.update(p.as_posix() for p in PurePosixPath(f).parents)
    return {r for r in rel if r in present}


def _pathspec(files: Iterable[str]) -> tuple[str, ...]:
    return (*(f":(glob){p}" for p in files), *(f":(exclude,glob){p}" for p in EXCLUDED_FILES))


# 交给 git 的 pathspec：磁盘上哪些路径是资源文件由 git 按它判断；还没落盘的文件（导入的包）只能用 is_attachment
# 预先判断，两者由测试对照
RESOURCE_PATHSPEC = _pathspec(RESOURCE_FILES)
# 本模块写了一半的临时文件（_write）：只有 store 写的文件会有，附件旁边的 *.tmp 是 agent 自己的
PARTIAL_WRITE_PATHSPEC = _pathspec(f"{p}{_TMP}" for p in STORE_FILES)


_GIT_EXIT_SECONDS = 5.0


@dataclass(frozen=True)
class CorruptResource:
    """一个读不出来的资源文件（多半是绕过工具改坏的，见模块说明）。列出资源的地方跳过它、单独交出来：坏的只是这一项，
    不连累同一目录里的其他资源，也不让读到它的每次运行都失败；它照样以错误报出（日志、交给模型的说明），等人或 agent
    修复（repair：怎么修因种类而异）或删除它。"""

    # 资源的种类，如 "skill"、"MCP server"
    kind: str
    owner: str | None
    # 由文件位置决定的名称（目录名或文件名）：内容坏了也知道是哪一项
    name: str
    path: Path
    problem: str
    # 怎么修好它，接在「cannot be used until it is」后面，如 skill 的 "rewritten or deleted"（write_skill 整篇覆盖）、
    # MCP server 的 "removed and added again"（没有覆盖写入的入口）
    repair: str

    def describe(self) -> str:
        return (
            f"{scope_of(self.owner)} {self.kind} {self.name!r} is corrupt and cannot be used until it is "
            f"{self.repair}: {self.problem}"
        )


def _reported(corrupt: list[CorruptResource]) -> list[CorruptResource]:
    """交出去的坏资源，每次都记 error（只在交出的地方记：只要读得出来的那些的列出不记，一次读取不记两遍）。"""
    for c in corrupt:
        log.error(
            "corrupt resource",
            extra={"kind": c.kind, "owner": c.owner, "resource": c.name, "error": c.problem},
        )
    return corrupt


@dataclass(frozen=True)
class ResourceFiles[T]:
    """一种每项一个文件的资源（skill、MCP server）在 owner 目录下的存放与列出：读得出来的，和读不出来的
    （load 抛 CorruptState 即 CorruptResource，坏的只是这一项，不连累别的）。各 store 只给出自己的差别。"""

    repo: ResourceRepo
    # 种类与修复方式，见 CorruptResource
    kind: str
    repair: str
    # owner 目录下的子目录，与其中每一项的文件（glob，相对子目录）
    subdir: str
    pattern: str
    # 由文件位置决定的名称（内容坏了也知道是哪一项）
    name: Callable[[Path], str]
    load: Callable[[str | None, Path], T]

    def dir(self, owner: str | None) -> Path:
        return self.repo.owner_dir(owner) / self.subdir

    def _scan(self, owner: str | None) -> tuple[list[T], list[CorruptResource]]:
        loaded: list[T] = []
        corrupt: list[CorruptResource] = []
        # 悬空的符号链接不算一项（与按名称读、删的 exists() 判断一致）：报成坏项也删不掉
        for path in sorted(p for p in self.dir(owner).glob(self.pattern) if p.exists()):
            try:
                loaded.append(self.load(owner, path))
            except CorruptState as e:
                corrupt.append(CorruptResource(self.kind, owner, self.name(path), path, str(e), self.repair))
        return loaded, corrupt

    def list(self, owner: str | None) -> list[T]:
        """owner 名下读得出来的；坏的见 corrupt_in。"""
        return self._scan(owner)[0]

    def corrupt_in(self, owner: str | None) -> list[CorruptResource]:
        """owner 名下读不出来的（管理界面列出，供修复或删除）。"""
        return _reported(self._scan(owner)[1])

    def index(self, agent_id: str) -> tuple[list[T], list[CorruptResource]]:
        """agent 的索引要的两个目录（它私有的在前，然后公共的）各扫一次：读得出来的（交给调用方按同名遮蔽合并），和
        两边读不出来的。坏的不参与遮蔽——读不出来就不知道它是什么。"""
        private, private_corrupt = self._scan(agent_id)
        public, public_corrupt = self._scan(None)
        return private + public, _reported(private_corrupt + public_corrupt)


@dataclass(frozen=True)
class Revision:
    rev: str
    # "user" 或做出修改的 agent 的 id（可能已被删除，只作审计展示）
    author: str
    at: datetime
    message: str


def _read_optional(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


# 一个路径写入前的样子：不存在为 None，文件为其字节，目录为其中每个文件的相对路径与字节
Snapshot = bytes | dict[str, bytes] | None


def _snapshot(path: Path) -> Snapshot:
    if path.is_dir():
        return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    return path.read_bytes() if path.exists() else None


def _restore_snapshot(path: Path, snapshot: Snapshot) -> None:
    _remove(path)
    if isinstance(snapshot, bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snapshot)
    elif isinstance(snapshot, dict):
        for name, data in snapshot.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + _TMP)
    # 临时文件不跟随符号链接：agent 用 shell 放在那里的链接会把内容写到别处（写入路径本身由调用方查，
    # 见 check_no_links）。上次留下的同名文件是写了一半的残留，先删掉
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    tmp.replace(path)


class RevisionHistory:
    """资源文件的版本历史，只读，转发给 ResourceRepo。HTTP 层拿到的是这个对象而不是仓库本身，所以在运行时也写不了；
    写入（包括恢复到某一版）一律经各 store，owner 由资源仓库在锁里检查（agents.active）。"""

    def __init__(self, repo: ResourceRepo):
        self._repo = repo

    async def history(self, path: Path, limit: int) -> list[Revision]:
        return await self._repo.history(path, limit)

    async def contents_at(self, revs: Sequence[str], path: Path) -> list[str]:
        return await self._repo.contents_at(revs, path)

    async def read_at(self, rev: str, path: Path) -> str:
        return await self._repo.read_at(rev, path)


class ResourceRepo:
    """写入（apply / update）默认在锁里、写入之前检查涉及的每个 owner 还可用（check_owner，agents.active）：owner 由路径推出
    （owner_dir 的布局），不靠各 store 记得传。只有创建 agent（它的行还不存在）与删除 agent 的资源目录（它已是
    正在删除）显式传 unchecked=True。"""

    def __init__(self, root: Path, check_owner: OwnerCheck):
        self.root = root
        self._check_owner = check_owner
        self._lock = asyncio.Lock()
        self._ready = False
        # 已记过 warning 的留在历史外的文件（见 _left_out）
        self._reported_left_out: set[str] = set()

    async def check_owner(self, owner: str | None) -> None:
        """不写入、不加锁地先查一次 owner 还可用（如新增 MCP server 在连接验证之前）；写入时锁里那次才是准的。"""
        await self._check_owner(owner)

    def _owner_of(self, path: Path) -> str | None:
        """路径所属的 owner（owner_dir 的反向）；不在任何 owner 目录下的路径不是资源，直接报错。

        按 relative() 解析后的路径判断（与提交用的是同一个相对路径）：经符号链接的目录写到别的 owner 名下时，查的是实际
        被写的那个 owner；链接所在的 agent 若正在删除，它的目录随后在同一把锁里被删掉，不留孤儿。"""
        parts = self.relative(path).split("/")
        if parts[0] == _PUBLIC:
            return None
        if parts[0] == _AGENTS and len(parts) >= 2 and AGENT_ID.fullmatch(parts[1]):
            return parts[1]
        raise ValueError(f"{path} is not inside an owner directory of the resource repository")

    async def _check_owners_locked(self, paths: Iterable[Path]) -> None:
        for owner in sorted({self._owner_of(p) for p in paths}, key=lambda o: o or ""):
            await self._check_owner(owner)

    def owner_dir(self, owner: str | None) -> Path:
        """资源归属的目录：None 为公共，否则为该 agent 私有。布局只在这里定义。

        所有资源路径都经过这里，所以 owner 的格式在这里校验：不合法的 id（如 `..`）不能构造出越界的路径。
        agent 是否存在不在这里判断——那是数据库里的事实，由调用方经 AgentService 确认。
        """
        if owner is None:
            return self.root / _PUBLIC
        if not AGENT_ID.fullmatch(owner):
            raise InvalidInput(f"invalid agent id {owner!r}")
        return self.root / _AGENTS / owner

    def agent_owners(self) -> list[str]:
        """有私有资源目录的 agent id。"""
        root = self.root / _AGENTS
        return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []

    def relative(self, path: Path) -> str:
        """仓库内的相对路径；不在仓库里的路径直接报错（防止越界读取）。"""
        resolved = path.resolve()
        root = self.root.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"{path} is outside the resource repository {self.root}")
        return resolved.relative_to(root).as_posix()

    async def _git(self, *args: str, check: bool = True) -> tuple[int, str]:
        code, out = await self._git_bytes(*args, check=check)
        return code, out.decode()

    async def _git_bytes(
        self, *args: str, check: bool = True, stdin: bytes | None = None
    ) -> tuple[int, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.root),
            # 不受部署机全局 git 配置影响：hook 与签名都可能让每次提交失败
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *args,
            stdin=None if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await proc.communicate(stdin)
        except BaseException:
            # 被取消时不留下继续运行的 git（它会占着锁，或在调用方以为失败之后才提交）。
            # 先 SIGTERM：git 会自己删掉锁文件；不退出才 SIGKILL（留下的锁由下次启动的 recover 清理）
            # 被取消后还要等它退出：清理不能被取消打断
            if proc.returncode is None:
                proc.terminate()
                with anyio.CancelScope(shield=True):
                    with anyio.move_on_after(_GIT_EXIT_SECONDS):
                        await proc.wait()
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
            raise
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {err.decode().strip()}")
        return proc.returncode or 0, out

    async def ensure(self) -> None:
        """首次使用时初始化仓库；git 不可用时直接报错。"""
        if self._ready:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            await self._git("init", "-q", "-b", "main")
            await self._git("config", "user.name", SYSTEM)
            await self._git("config", "user.email", f"{SYSTEM}@localhost")
            await self._git("commit", "-q", "--allow-empty", "-m", "init resources")
            log.info("resource repository initialised", extra={"root": str(self.root)})
        self._ready = True

    async def recover(self) -> None:
        """启动时（worker 开始之前、没有任何写入在进行）把仓库收敛到干净状态。

        进程在写入或 git 运行中被杀会留下：
        - .git 下遗留的锁（index.lock、refs/…/*.lock）：此刻不可能有别的 git 在用，删除并记录；
        - 写了一半的资源文件（`<资源文件>.tmp`，见 _write）：按定义不完整，删除并记录，不进入历史；
        - 写完但没来得及提交的变化：与运行期的外部写入一样提交为一次 reconcile。
        """
        async with self._lock:
            await self.ensure()
            for stale in [*(self.root / ".git").rglob("*.lock"), *await self._partial_writes()]:
                stale.unlink()
                log.warning("removed leftover of a killed process", extra={"path": str(stale)})
            await self._reconcile_locked()

    async def _reconcile_locked(self) -> None:
        """把资源文件上不经本模块的变化（外部写入、被杀的进程写完未提交的）单独提交为一次 reconcile。"""
        await self.ensure()
        # 只列资源文件：status 对没有匹配的 pathspec 不报错（add 会），布局之外的路径 git 根本不会去看
        # --no-renames：每条记录只有「XY 路径」一段。否则暂存区里的重命名（进程死在上一次 reconcile 的 add 与
        # commit 之间）会多带一个原路径，按单路径解析就会切坏它
        _, status = await self._git(
            "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--", *RESOURCE_PATHSPEC
        )
        # 状态码第二位是工作区相对暂存区的变化；已经完整暂存的（如上次留下的删除）不再 add：路径不在工作区时 add 会报错
        unstaged = self._within_size_limit(
            [entry[3:] for entry in status.split("\0") if entry and entry[1] != " "]
        )
        if unstaged:
            await self._stage_each_possible(unstaged)
        # 要提交什么由 add 之后的暂存区决定，而不是 add 之前的状态：add 会改变它（暂存过又在工作区删掉的新文件，
        # add 之后既不在 HEAD 也不在暂存区，不能再交给 commit）
        _, staged = await self._git(
            "diff", "--cached", "--name-only", "-z", "--no-renames", "--", *RESOURCE_PATHSPEC
        )
        paths = [rel for rel in staged.split("\0") if rel]
        if not paths:
            return
        await self._git_on_paths(
            paths, "commit", "-q", "-m", RECONCILE_MESSAGE, f"--author={SYSTEM} <{SYSTEM}@muliao>"
        )
        log.warning("committed resource changes made outside the resource tools", extra={"paths": paths})

    def _within_size_limit(self, paths: list[str]) -> list[str]:
        """去掉超过 MAX_EXTERNAL_FILE_BYTES 的文件（删除照常记录）。"""
        kept = []
        for rel in paths:
            path = self.root / rel
            size = path.lstat().st_size if path.is_file() and not path.is_symlink() else 0
            if size <= MAX_EXTERNAL_FILE_BYTES:
                kept.append(rel)
            else:
                self._left_out(rel, "too large to version", size=size, limit=MAX_EXTERNAL_FILE_BYTES)
        return kept

    def _left_out(self, rel: str, reason: str, **detail: object) -> None:
        """记下一个留在历史外的资源文件：每次写入都会 reconcile，同一个文件在进程里只记一次 warning。"""
        if rel not in self._reported_left_out:
            self._reported_left_out.add(rel)
            log.warning(f"resource file {reason}, left out of history", extra={"path": rel, **detail})

    async def _stage_each_possible(self, paths: list[str]) -> None:
        """暂存这些路径；一起暂存失败（如某个文件读不了）时逐个暂存，跳过失败的并记 warning：
        一个目录里的坏文件不能让所有资源写入失败。"""
        try:
            await self._git_on_paths(paths, "add", "-A")
        except RuntimeError:
            # 全局性的失败（如锁被占）在这里与坏文件分不开，也会逐个记成留在历史外；它在随后的写入里照样报错。
            # 反过来不行：把「全都失败」当全局失败抛出，几个坏文件就会让所有写入失败
            for rel in paths:
                try:
                    await self._git("add", "-A", "--", _literal(rel))
                except RuntimeError as e:
                    self._left_out(rel, "cannot be staged", error=str(e))

    async def _git_on_paths(self, paths: Sequence[str], *args: str) -> None:
        """对一组路径运行 git：路径经 stdin 传入，文件再多也不会超出命令行长度上限（那会让所有写入失败）。"""
        # 空的 pathspec 列表对 git 是「整棵树」：会把排除的、超限的文件都带进历史
        if not paths:
            raise ValueError(f"git {args[0]} needs at least one path")
        await self._git_bytes(
            *args,
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            stdin="".join(f"{_literal(p)}\0" for p in paths).encode(),
        )

    async def _partial_writes(self) -> list[Path]:
        """资源文件写了一半的临时文件（见 _write）。由 git 按同一组 pattern 列出：不跟随符号链接，不进入嵌套仓库。"""
        _, out = await self._git("ls-files", "--others", "-z", "--", *PARTIAL_WRITE_PATHSPEC)
        return [self.root / rel for rel in out.split("\0") if rel]

    async def apply(
        self,
        changes: Mapping[Path, str | None],
        message: str,
        author: str,
        *,
        unchecked: bool = False,
    ) -> str | None:
        """写入（内容为 None 表示删除该文件或目录）并提交，原子地：要么提交成功，要么什么都没发生。

        写入之前在锁里检查涉及的 owner 还可用（不可用即拒绝这次写入），unchecked=True 时跳过（见类说明）。"""
        async with self._lock:
            if not unchecked:
                await self._check_owners_locked(changes)
            # 外部写入先单独记一版，不并进这次作者的提交
            await self._reconcile_locked()
            return await self._apply_locked(changes, message, author)

    async def apply_planned(
        self,
        plan: Callable[[TrackedFiles], Awaitable[Mapping[Path, str | None]]],
        message: str,
        author: str,
    ) -> str | None:
        """同 apply，但要写什么在锁里、收敛外部写入之后才算出来：plan 可以看当前状态（文件系统，以及经它的参数
        看某个目录里在版本管理中的文件）再决定写入，或抛领域错误拒绝。用于一次替换一组文件（如导入覆盖一个 skill），
        判断与写入之间插不进别的写入。owner 的检查同 apply（总是检查）。"""
        async with self._lock:
            await self._reconcile_locked()
            changes = await plan(self._tracked_under)
            await self._check_owners_locked(changes)
            return await self._apply_locked(changes, message, author)

    def check_no_links(self, path: Path) -> None:
        """路径在仓库里且不经过任何符号链接（按字面拼出的就是它实际指向的）。写入的路径来自外部输入时（导入的包）
        先查：agent 用 shell 在 skill 目录里建的链接会把写入引到别的 skill、别的 owner 名下或仓库外。"""
        try:
            lexical = path.relative_to(self.root)
        except ValueError:
            raise InvalidInput(f"{path} is outside the resource repository") from None
        if path.resolve() != self.root.resolve() / lexical:
            raise InvalidInput(
                f"{lexical.as_posix()} goes through a symbolic link in the resource repository"
            )

    async def versioned_files(self, directory: Path) -> dict[str, VersionedFile]:
        """目录里在版本管理中的文件（相对这个目录的路径 → 内容）：先收敛外部写入，再按 HEAD 读，所以就是这个目录
        最新的一版历史。留在历史外的（不在白名单里的、超过上限的）不在里面；已在历史里、后来变得超限的文件是它在
        历史里的最后一版（见 MAX_EXTERNAL_FILE_BYTES）。只含普通文件，符号链接不算。目录本身不能经过符号链接
        （check_no_links）：那样读到的是另一个目录的历史。"""
        self.check_no_links(directory)
        prefix = self.relative(directory)
        async with self._lock:
            await self._reconcile_locked()
            # ls-tree 的路径参数按字面前缀匹配（不是 pathspec，没有通配与魔法）
            _, listing = await self._git("ls-tree", "-r", "-z", "HEAD", "--", f"{prefix}/")
            # 每条「<mode> <type> <oid>\t<path>」；只要普通文件（100644 / 100755）
            entries = []
            for entry in listing.split("\0"):
                if not entry:
                    continue
                meta, path = entry.split("\t", 1)
                mode, kind, oid = meta.split()
                if kind == "blob" and mode in (_REGULAR, _EXECUTABLE):
                    entries.append((path.removeprefix(f"{prefix}/"), mode, oid))
            contents = await self._cat_batch([oid for _, _, oid in entries])
        return {
            path: VersionedFile(data, mode == _EXECUTABLE)
            for (path, mode, _), data in zip(entries, contents, strict=True)
        }

    async def _cat_batch(self, objects: Sequence[str]) -> list[bytes]:
        """一次 git cat-file --batch 读出这些对象（oid 或 `<rev>:<path>`）的内容，与 objects 一一对应，不按对象逐个起进程。
        某个对象不存在时报 NotFound。调用方持锁。"""
        if not objects:
            return []
        _, out = await self._git_bytes(
            "cat-file", "--batch", stdin="".join(f"{o}\n" for o in objects).encode()
        )
        contents = []
        pos = 0
        for obj in objects:
            # 每个对象一行头「<oid> <type> <size>」，随后 size 字节内容与一个换行；找不到时只有「<name> missing」
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            if header[-1] == b"missing":
                raise NotFound(f"{obj} does not exist")
            if len(header) != 3 or not header[2].isdigit():
                raise RuntimeError(f"git cat-file gave an unexpected header for {obj}: {header!r}")
            size = int(header[2])
            contents.append(out[end + 1 : end + 1 + size])
            pos = end + 1 + size + 1
        return contents

    async def _tracked_under(self, directory: Path) -> list[Path]:
        _, out = await self._git("ls-files", "-z", "--", _literal(self.relative(directory)))
        return [self.root / rel for rel in out.split("\0") if rel]

    async def update(
        self,
        path: Path,
        transform: Callable[[str | None], str | None],
        message: str,
        author: str,
    ) -> str | None:
        """读取当前内容（不存在为 None）、变换、写入并提交，整个过程在同一把锁里：并发的修改互不覆盖。

        transform 返回新内容（None 表示删除），或抛出领域错误拒绝这次修改。owner 的检查同 apply（总是检查）。
        """
        async with self._lock:
            await self._check_owners_locked([path])
            # 先收敛外部写入再读：读到的就是刚提交的版本，变换的结果不会悄悄盖掉一次外部写入
            await self._reconcile_locked()
            return await self._apply_locked({path: transform(_read_optional(path))}, message, author)

    async def _apply_locked(
        self, changes: Mapping[Path, str | None], message: str, author: str
    ) -> str | None:
        rel = [self.relative(p) for p in changes]
        # 写入之前的内容，失败时直接在文件系统上还原：不依赖 git（失败的往往正是 git，比如锁被占用）
        before = {path: _snapshot(path) for path in changes}
        try:
            for path, content in changes.items():
                if content is None:
                    _remove(path)
                    self._prune_inside_skill(path)
                else:
                    _write(path, content)
            # 删除一个从未提交过的路径（如没有资源目录的 agent）：没有可记录的变化
            before_add = await self._tracked_among(rel)
            tracked = [r for r in rel if (self.root / r).exists() or r in before_add]
            if not tracked:
                return None
            # 路径按字面匹配（导入的文件名可能带通配符），经 stdin 传：一次写入的文件可以很多（覆盖导入时删掉旧 skill
            # 的全部文件）。ls-files 与 diff 不接受 stdin 的路径：它们只给公共上级目录，再在这里取交集
            await self._git_on_paths(tracked, "add", "-A")
            after_add = await self._tracked_among(rel)
            for path, r in zip(changes, rel, strict=True):
                # git add 对它不管的路径（如上级目录是一个嵌套仓库）照样返回 0，不能当成「没有变化」悄悄成功
                if changes[path] is not None and r not in after_add:
                    raise RuntimeError(
                        f"{r} is not versioned by git after adding it; is a parent directory a nested repository?"
                    )
            _, staged = await self._git("diff", "--cached", "--name-only", "-z", *_under_common(tracked))
            if not _covered(tracked, staged.split("\0")):
                return None
            await self._git_on_paths(
                tracked, "commit", "-q", "-m", message, f"--author={author} <{author}@muliao>"
            )
        except BaseException as error:
            # 「报错」等于「没有生效」：还原失败本身也必须可见，不能声称已回滚
            await self._restore(before, rel, error)
            raise
        # 提交已成功：之后的步骤失败不能触发回滚（那会把成功报成失败）
        _, rev = await self._git("rev-parse", "HEAD")
        log.info("resource committed", extra={"paths": rel, "author": author, "rev": rev.strip()})
        return rev.strip()

    async def _tracked_among(self, rel: Sequence[str]) -> set[str]:
        """这些路径里在暂存区中的：文件本身在，或（目录）它下面有文件在。一次 ls-files，不按路径逐个起进程。"""
        _, out = await self._git("ls-files", "-z", *_under_common(rel))
        return _covered(rel, out.split("\0"))

    def _prune_inside_skill(self, path: Path) -> None:
        """删掉一个 skill 目录里的文件后，删掉它在 skill 目录里、现在空了的上级目录（不含 skill 目录本身）：git 不记目录，
        留着会让同一次写入里同名的新文件（覆盖导入时 x.md/ 变成 x.md）写不进去。布局里的目录（public/、skills/……）不动。"""
        parts = self.relative(path).split("/")
        # skill 目录是 public/skills/<name> 或 agents/<id>/skills/<name>
        depth = 3 if parts[0] == _PUBLIC else 4
        if len(parts) <= depth or parts[depth - 2] != "skills":
            return
        for end in range(len(parts) - 1, depth, -1):
            parent = self.root.joinpath(*parts[:end])
            if not parent.is_dir() or any(parent.iterdir()):
                return
            parent.rmdir()

    def _prune_empty_parents(self, path: Path) -> None:
        """删掉一次回滚的新路径为它建出来、现在空了的上级目录（如新 agent 的 agents/<id>/），直到仓库根目录：
        空目录在 git 里不存在，留着只会被启动检查误报成已删除 agent 的残留。"""
        root = self.root.resolve()
        for parent in path.resolve().parents:
            # 不是目录：这个上级已被还原成文件（见 _restore）
            if (
                parent == root
                or not parent.is_relative_to(root)
                or not parent.is_dir()
                or any(parent.iterdir())
            ):
                return
            parent.rmdir()

    async def _restore(
        self, before: Mapping[Path, Snapshot], rel: Sequence[str], error: BaseException
    ) -> None:
        try:
            # 先删掉所有路径上现在的东西（深的先），再写回快照，最后修剪新路径建出来的空目录：同一次写入里一个路径可能
            # 从目录变成文件（覆盖导入时 x.md/inner.md 变成 x.md）或反过来，逐项还原会撞上对方已写下的东西
            for path in sorted(before, key=lambda p: len(p.parts), reverse=True):
                _remove(path)
            for path, snapshot in before.items():
                _restore_snapshot(path, snapshot)
            for path, snapshot in before.items():
                if snapshot is None:
                    self._prune_empty_parents(path)
        except Exception as restore_error:
            log.error(
                "resource write failed and could not be rolled back; recover on next start will commit it: %r",
                restore_error,
                extra={"paths": list(rel), "error": repr(error)},
            )
            raise restore_error from error
        # 索引里可能已暂存了新内容：撤销暂存失败无妨，下一次 add 会重新暂存当时的文件。写入被取消时也做（屏蔽取消）
        code: int | None = None
        with anyio.CancelScope(shield=True), anyio.move_on_after(_GIT_EXIT_SECONDS):
            code, _ = await self._git("reset", "-q", "--", *(_literal(r) for r in rel), check=False)
        log.warning(
            "resource write rolled back",
            extra={"paths": list(rel), "error": repr(error), "unstaged": code == 0},
        )

    async def history(self, path: Path, limit: int) -> list[Revision]:
        """这个文件的版本，新的在前。"""
        async with self._lock:
            await self.ensure()
            _, out = await self._git(
                "log", f"-n{limit}", f"--format=%H{_FIELD}%an{_FIELD}%aI{_FIELD}%s", "--", self.relative(path)
            )
        revisions = []
        for line in out.splitlines():
            rev, author, at, message = line.split(_FIELD, 3)
            revisions.append(Revision(rev, author, datetime.fromisoformat(at), message))
        return revisions

    async def contents_at(self, revs: Sequence[str], path: Path) -> list[str]:
        """这个文件在每个版本（history 列出的）时的内容，与 revs 一一对应：一次 git cat-file --batch 读完，
        不按版本逐个起进程。某个版本里没有这个文件时报错。"""
        relative = self.relative(path)
        async with self._lock:
            await self.ensure()
            contents = await self._cat_batch([f"{rev}:{relative}" for rev in revs])
        return [c.decode() for c in contents]

    async def read_at(self, rev: str, path: Path) -> str:
        """某个版本时的文件内容；那个版本里没有这个文件时报错。"""
        # 版本号来自外部输入：只接受完整的提交哈希，不让它被 git 当成选项
        if not _REV.fullmatch(rev):
            raise InvalidInput(f"invalid revision {rev!r}")
        async with self._lock:
            await self.ensure()
            code, out = await self._git("show", f"{rev}:{self.relative(path)}", check=False)
        if code != 0:
            raise NotFound(f"{self.relative(path)} does not exist at revision {rev}")
        return out
