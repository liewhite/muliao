"""agent 能改的文本资源（指令、公共 prompt、记忆、skill、MCP 配置）存成文件，用 git 管理版本。

布局（resources 根目录就是一个 git 仓库）：

    public/                 公共资源：PROMPT.md、skills/<name>/SKILL.md、mcp/<name>.json
    agents/<agent id>/      agent 私有资源：PROMPT.md、MEMORY.md、skills/<name>/SKILL.md、mcp/<name>.json

每次修改写文件后提交一次，作者是 "user" 或做出修改的 agent 的 id。历史与回滚都来自 git：
回滚就是把旧版本的内容写回并作为新的一次提交，历史只增不改。

写文件与提交在同一个临界区里（apply）。提交失败时错误向上传播（可见）；已写未提交的文件，以及进程中途被杀
留下的 git 锁，由启动时的 recover 统一收敛（提交为一次 reconcile，记录涉及的路径）。
secret 不在这里：它们加密存在数据库的 vault 里，不需要版本管理。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from muliao.errors import InvalidInput, NotFound

log = logging.getLogger(__name__)

USER = "user"
# 系统自己做的提交（初始化、启动时的收敛）
SYSTEM = "muliao"
_PUBLIC = "public"
_AGENTS = "agents"
_FIELD = "\x1f"
_REV = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Revision:
    rev: str
    # "user" 或做出修改的 agent 的 id（可能已被删除，只作审计展示）
    author: str
    at: datetime
    message: str


class ResourceRepo:
    def __init__(self, root: Path):
        self.root = root
        self._lock = asyncio.Lock()
        self._ready = False

    def owner_dir(self, owner: str | None) -> Path:
        """资源归属的目录：None 为公共，否则为该 agent 私有。布局只在这里定义。"""
        return self.root / _PUBLIC if owner is None else self.root / _AGENTS / owner

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
        out, err = await proc.communicate()
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

        - 上次进程在 git 运行中被杀留下的 index.lock：此刻不可能有别的 git 在用它，删除并记录；
        - 写了文件但没来得及提交的变化（进程被杀、提交失败）：整体提交为一次 reconcile，作者记为系统，
          并记录涉及的路径——修改者已无从得知，如实标出。
        """
        async with self._lock:
            await self.ensure()
            lock = self.root / ".git" / "index.lock"
            if lock.exists():
                lock.unlink()
                log.warning(
                    "removed stale git index lock left by a killed process", extra={"root": str(self.root)}
                )
            await self._git("add", "-A")
            _, staged = await self._git("diff", "--cached", "--name-only")
            if not staged.strip():
                return
            await self._git(
                "commit", "-q", "-m", "reconcile uncommitted changes", f"--author={SYSTEM} <{SYSTEM}@muliao>"
            )
        log.warning("committed resource changes left uncommitted", extra={"paths": staged.split()})

    async def apply(self, changes: Mapping[Path, str | None], message: str, author: str) -> str | None:
        """在一个临界区里写入（内容为 None 表示删除该文件或目录）并提交；没有变化时返回 None。

        写与提交在同一把锁里：并发的修改不会被别人的提交带走、记错作者。
        """
        async with self._lock:
            await self.ensure()
            rel = [self.relative(p) for p in changes]
            for path, content in changes.items():
                if content is None:
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(path.name + ".tmp")
                    tmp.write_text(content, encoding="utf-8")
                    tmp.replace(path)
            await self._git("add", "-A", "--", *rel)
            unchanged, _ = await self._git("diff", "--cached", "--quiet", "--", *rel, check=False)
            if unchanged == 0:
                return None
            await self._git("commit", "-q", "-m", message, f"--author={author} <{author}@muliao>", "--", *rel)
            _, rev = await self._git("rev-parse", "HEAD")
        log.info("resource committed", extra={"paths": rel, "author": author, "rev": rev.strip()})
        return rev.strip()

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
