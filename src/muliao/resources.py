"""agent 能改的文本资源（指令、公共 prompt、记忆、skill、MCP 配置）存成文件，用 git 管理版本。

布局（resources 根目录就是一个 git 仓库）：

    public/                 公共资源：PROMPT.md、skills/<name>/SKILL.md、mcp/<name>.json
    agents/<agent id>/      agent 私有资源：PROMPT.md、MEMORY.md、skills/<name>/SKILL.md、mcp/<name>.json

每次修改写文件后提交一次，作者是 "user" 或做出修改的 agent 的 id。历史与回滚都来自 git：
回滚就是把旧版本的内容写回并作为新的一次提交，历史只增不改。

写入是原子的：读取—变换—写入—提交在同一把锁里（update），任何一步失败都把涉及的路径在文件系统上还原成写入前
的样子（不依赖 git），所以「报错」等于「没有生效」，调用方可以放心重试；还原本身失败时记 error 并一起抛出。

资源文件就是上面布局里列出的那些（RESOURCE_FILES，唯一定义），版本管理只管它们。

仓库也会被绕过本模块写入：agent 有 shell 与文件工具，能直接改资源文件（read_skill 会给出 skill 目录）；进程在写入
中途被杀也会留下写完未提交的变化。两者用同一个机制收敛：每次写入前、以及启动时（recover），把资源文件上不经本模块的
变化先单独提交为一次 reconcile（作者记为系统，表示「来源不明的外部写入」）。这样外部写入最迟在下一次任何资源写入时
进入历史，也不会被并进下一个作者的提交里。布局之外的东西（agent 放进目录的脚本、虚拟环境、嵌套的 git 仓库、自己的
临时文件）不进历史：历史只增不改，进去就删不掉，也不能让一个 agent 目录里的杂物让所有资源写入失败。
secret 不在这里：它们加密存在数据库的 vault 里，不需要版本管理。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from muliao.errors import InvalidInput, NotFound

log = logging.getLogger(__name__)

USER = "user"
# 系统自己做的提交（初始化、启动时的收敛）
SYSTEM = "muliao"
RECONCILE_MESSAGE = "reconcile changes made outside the resource tools"
_PUBLIC = "public"
_AGENTS = "agents"
_FIELD = "\x1f"
_REV = re.compile(r"^[0-9a-f]{40}$")
# 与 ids.new_agent_id 的格式一致
_AGENT_ID = re.compile(r"^[0-9a-f]{12}$")
_TMP = ".tmp"
# 资源文件（相对仓库根目录的 glob，`*` 不跨目录）：见模块说明的布局。reconcile 与 recover 只处理它们。
# 文件名由各 store 拥有（MEMORY_FILE、SKILL_FILE……），这里是副本，由 test_every_file_the_stores_write_is_in_the_resource_layout 守护
RESOURCE_FILES = (
    "public/PROMPT.md",
    "public/skills/*/SKILL.md",
    "public/mcp/*.json",
    "agents/*/PROMPT.md",
    "agents/*/MEMORY.md",
    "agents/*/skills/*/SKILL.md",
    "agents/*/mcp/*.json",
)

# 交给 git 的 pathspec：哪些路径是资源文件只由 git 按它判断，不另写一套匹配
RESOURCE_PATHSPEC = tuple(f":(glob){p}" for p in RESOURCE_FILES)


_GIT_EXIT_SECONDS = 5.0


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
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


class ResourceRepo:
    def __init__(self, root: Path):
        self.root = root
        self._lock = asyncio.Lock()
        self._ready = False

    def owner_dir(self, owner: str | None) -> Path:
        """资源归属的目录：None 为公共，否则为该 agent 私有。布局只在这里定义。

        所有资源路径都经过这里，所以 owner 的格式在这里校验：不合法的 id（如 `..`）不能构造出越界的路径。
        agent 是否存在不在这里判断——那是数据库里的事实，由调用方经 AgentService 确认。
        """
        if owner is None:
            return self.root / _PUBLIC
        if not _AGENT_ID.fullmatch(owner):
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await proc.communicate()
        except BaseException:
            # 被取消时不留下继续运行的 git（它会占着锁，或在调用方以为失败之后才提交）。
            # 先 SIGTERM：git 会自己删掉锁文件；不退出才 SIGKILL（留下的锁由下次启动的 recover 清理）
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), _GIT_EXIT_SECONDS)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            raise
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {err.decode().strip()}")
        return proc.returncode or 0, out.decode()

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
        unstaged = [entry[3:] for entry in status.split("\0") if entry and entry[1] != " "]
        if unstaged:
            await self._git("add", "-A", "--", *unstaged)
        # 要提交什么由 add 之后的暂存区决定，而不是 add 之前的状态：add 会改变它（暂存过又在工作区删掉的新文件，
        # add 之后既不在 HEAD 也不在暂存区，不能再交给 commit）
        _, staged = await self._git(
            "diff", "--cached", "--name-only", "-z", "--no-renames", "--", *RESOURCE_PATHSPEC
        )
        paths = [rel for rel in staged.split("\0") if rel]
        if not paths:
            return
        await self._git(
            "commit", "-q", "-m", RECONCILE_MESSAGE, f"--author={SYSTEM} <{SYSTEM}@muliao>", "--", *paths
        )
        log.warning("committed resource changes made outside the resource tools", extra={"paths": paths})

    async def _partial_writes(self) -> list[Path]:
        """资源文件写了一半的临时文件（见 _write）。由 git 按同一组 pattern 列出：不跟随符号链接，不进入嵌套仓库。"""
        _, out = await self._git(
            "ls-files", "--others", "-z", "--", *(f"{spec}{_TMP}" for spec in RESOURCE_PATHSPEC)
        )
        return [self.root / rel for rel in out.split("\0") if rel]

    async def apply(self, changes: Mapping[Path, str | None], message: str, author: str) -> str | None:
        """写入（内容为 None 表示删除该文件或目录）并提交，原子地：要么提交成功，要么什么都没发生。"""
        async with self._lock:
            # 外部写入先单独记一版，不并进这次作者的提交
            await self._reconcile_locked()
            return await self._apply_locked(changes, message, author)

    async def update(
        self, path: Path, transform: Callable[[str | None], str | None], message: str, author: str
    ) -> str | None:
        """读取当前内容（不存在为 None）、变换、写入并提交，整个过程在同一把锁里：并发的修改互不覆盖。

        transform 返回新内容（None 表示删除），或抛出领域错误拒绝这次修改。
        """
        async with self._lock:
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
                else:
                    _write(path, content)
            # 删除一个从未提交过的路径（如没有资源目录的 agent）：没有可记录的变化
            tracked = [r for r in rel if (self.root / r).exists() or await self._tracked(r)]
            if not tracked:
                return None
            await self._git("add", "-A", "--", *tracked)
            for path, r in zip(changes, rel, strict=True):
                # git add 对它不管的路径（如上级目录是一个嵌套仓库）照样返回 0，不能当成「没有变化」悄悄成功
                if changes[path] is not None and not await self._tracked(r):
                    raise RuntimeError(
                        f"{r} is not versioned by git after adding it; is a parent directory a nested repository?"
                    )
            unchanged, _ = await self._git("diff", "--cached", "--quiet", "--", *tracked, check=False)
            if unchanged == 0:
                return None
            await self._git(
                "commit", "-q", "-m", message, f"--author={author} <{author}@muliao>", "--", *tracked
            )
        except BaseException as error:
            # 「报错」等于「没有生效」：还原失败本身也必须可见，不能声称已回滚
            await self._restore(before, rel, error)
            raise
        # 提交已成功：之后的步骤失败不能触发回滚（那会把成功报成失败）
        _, rev = await self._git("rev-parse", "HEAD")
        log.info("resource committed", extra={"paths": rel, "author": author, "rev": rev.strip()})
        return rev.strip()

    async def _tracked(self, rel: str) -> bool:
        _, out = await self._git("ls-files", "--", rel)
        return bool(out.strip())

    async def _restore(
        self, before: Mapping[Path, Snapshot], rel: Sequence[str], error: BaseException
    ) -> None:
        try:
            for path, snapshot in before.items():
                _restore_snapshot(path, snapshot)
        except Exception as restore_error:
            log.error(
                "resource write failed and could not be rolled back; recover on next start will commit it: %r",
                restore_error,
                extra={"paths": list(rel), "error": repr(error)},
            )
            raise restore_error from error
        # 索引里可能已暂存了新内容：撤销暂存失败无妨，下一次 add 会重新暂存当时的文件
        code, _ = await self._git("reset", "-q", "--", *rel, check=False)
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
