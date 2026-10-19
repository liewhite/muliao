"""agent 能改的文本资源（指令、公共 prompt、记忆、skill、MCP 配置）存成文件，用 git 管理版本。

布局（resources 根目录就是一个 git 仓库）：

    public/                 公共资源：PROMPT.md、skills/<name>/SKILL.md、mcp/<name>.json
    agents/<agent id>/      agent 私有资源：PROMPT.md、MEMORY.md、skills/…、mcp/…

每次修改写文件后提交一次，作者是 "user" 或做出修改的 agent 的 id。历史与回滚都来自 git：
回滚就是把旧版本的内容写回并作为新的一次提交，历史只增不改。

写文件与提交是两步：提交失败时错误向上传播（可见），已写的文件留在工作区，下一次提交会把它一并带上（收敛）。
secret 不在这里：它们加密存在数据库的 vault 里，不需要版本管理。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from muliao.errors import InvalidInput, NotFound

log = logging.getLogger(__name__)

USER = "user"
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
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {err.decode().strip()}")
        return proc.returncode or 0, out.decode()

    async def ensure(self) -> None:
        """首次使用时初始化仓库；git 不可用时直接报错（启动时调用，尽早暴露）。"""
        if self._ready:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            await self._git("init", "-q", "-b", "main")
            await self._git("config", "user.name", "muliao")
            await self._git("config", "user.email", "muliao@localhost")
            await self._git("config", "commit.gpgsign", "false")
            await self._git("commit", "-q", "--allow-empty", "-m", "init resources")
            log.info("resource repository initialised", extra={"root": str(self.root)})
        self._ready = True

    async def commit(self, paths: Sequence[Path], message: str, author: str) -> str | None:
        """提交这些路径上的变化（含删除）；没有变化时返回 None。"""
        async with self._lock:
            await self.ensure()
            rel = [self.relative(p) for p in paths]
            await self._git("add", "-A", "--", *rel)
            changed, _ = await self._git("diff", "--cached", "--quiet", "--", *rel, check=False)
            if changed == 0:
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
