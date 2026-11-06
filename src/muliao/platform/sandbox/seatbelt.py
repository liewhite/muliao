"""macOS 的适配器：sandbox-exec 加一份按可写路径生成的 Seatbelt 配置（SBPL）。

配置：先放开一切（只限制写，读、网络、进程照旧），再禁止一切文件写，然后按 WritablePaths 的顺序逐条放开或收回（SBPL 里
后写的规则优先，与 WritablePaths 的「最后一条覆盖它的规则」同义）。沙箱由子进程继承，nohup、python 起的后代同样受限。
report 模式下「禁止」换成「放行并记进系统日志」：`log stream --predicate 'sender == "Sandbox"'` 能看到
`allow file-write-… <路径>`，后面附 `muliao write sandbox agent=<id>`。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from muliao.platform.sandbox import Category, Mode, WritablePaths
from muliao.platform.sandbox.common import (
    HOME_CACHE,
    HOME_PACKAGES,
    HOME_TOOL_STATE,
    HOME_TOOLCHAINS,
    platform_install,
    under,
)

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
# 日志里每条记录附带的说明，后面接 agent=<id>
LOG_MESSAGE = "muliao write sandbox"


def _quote(path: Path) -> str:
    text = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


class Seatbelt:
    def __init__(self, mode: Mode, home: Path | None = None):
        self.mode: Mode = mode
        self._home = Path.home() if home is None else home

    @staticmethod
    def supported() -> bool:
        return SANDBOX_EXEC.is_file()

    def paths(self, category: Category) -> tuple[Path, ...]:
        home = self._home
        match category:
            case Category.TEMP:
                # /tmp 与 /var/folders 是 /private 下的链接（WritablePaths 换成真实路径）；$TMPDIR 在 /var/folders 下
                return (Path("/tmp"), Path("/var/folders"))
            case Category.DEVICES:
                return (Path("/dev"),)
            case Category.USER_CACHE:
                return (home / HOME_CACHE, home / "Library/Caches")
            case Category.PACKAGES:
                return (Path("/opt/homebrew"), *under(home, HOME_PACKAGES))
            case Category.TOOLCHAINS:
                return under(home, HOME_TOOLCHAINS)
            case Category.TOOL_STATE:
                return under(home, HOME_TOOL_STATE)
            case Category.PLATFORM_INSTALL:
                return platform_install()

    def profile(self, writable: WritablePaths) -> str:
        message = f'(with message "{LOG_MESSAGE} agent={writable.owner}")'
        refuse = (
            f"(deny file-write* {message}"
            if self.mode == "enforce"
            else f"(allow file-write* (with report) {message}"
        )
        lines = ["(version 1)", "(allow default)", f"{refuse})"]
        for allowed, path in writable.rules:
            matcher = f"(subpath {_quote(path)})"
            lines.append(f"(allow file-write* {matcher})" if allowed else f"{refuse} {matcher})")
        return "\n".join(lines)

    def wrap(self, argv: Sequence[str], writable: WritablePaths) -> list[str]:
        return [os.fspath(SANDBOX_EXEC), "-p", self.profile(writable), *argv]
