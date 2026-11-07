"""macOS 的适配器：sandbox-exec 加一份按可写路径生成的 Seatbelt 配置（SBPL）。

配置：先放开一切（只限制写，读、网络、进程照旧；在沙箱里跑不起来的几种程序放到沙箱外执行），再禁止一切文件写，然后按 WritablePaths 的顺序逐条放开或收回（SBPL 里
后写的规则优先，与 WritablePaths 的「最后一条覆盖它的规则」同义）。沙箱由子进程继承，nohup、python 起的后代同样受限。
report 模式下「禁止」换成「放行并记进系统日志」：`allow file-write-… <路径>`，后面附 `muliao write sandbox agent=<id>`。
这些记录不落盘（`log show` 事后查不到），只能实时看：试跑期间另挂一个脱离会话的
`log stream --style compact --predicate 'eventMessage CONTAINS "muliao write sandbox"' >> 文件`。
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
# 在沙箱里跑不起来、只好放到沙箱外执行的程序（它们与它们的子进程写文件不受限，已知缺口）：
# - setuid 程序：macOS 不让沙箱里的进程执行（与写规则无关，只套 (allow default) 也一样），ps、top 常用；
#   sudo / su / login 要密码，放过也不会比不关沙箱时多做什么
# - 自己建沙箱的：沙箱里不能再套一层带规则的沙箱（sandbox_apply 报 EPERM），sandbox-exec 本身（swift build、测试里会用）与
#   Chromium 系浏览器（playwright）
_UNSANDBOXED_LITERALS = (
    "/bin/ps",
    "/usr/bin/top",
    "/usr/bin/atq",
    "/usr/bin/crontab",
    "/usr/bin/atrm",
    "/usr/bin/newgrp",
    "/usr/bin/su",
    "/usr/bin/batch",
    "/usr/bin/at",
    "/usr/bin/quota",
    "/usr/bin/sudo",
    "/usr/bin/login",
    "/usr/sbin/traceroute",
    "/usr/sbin/traceroute6",
    os.fspath(SANDBOX_EXEC),
)
_UNSANDBOXED_REGEX = r"/(chrome-headless-shell|Chromium|Google Chrome[^/]*)$"
# 日志里每条记录附带的说明，后面接 agent=<id>
LOG_MESSAGE = "muliao write sandbox"


def _quote(path: Path) -> str:
    text = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


class Seatbelt:
    def __init__(self, mode: Mode, home: Path):
        self.mode: Mode = mode
        self._home = home

    @staticmethod
    def supported() -> bool:
        return SANDBOX_EXEC.is_file()

    def paths(self, category: Category) -> tuple[Path, ...]:
        home = self._home
        match category:
            case Category.TEMP:
                # /tmp 与 /var/folders 是 /private 下的链接（WritablePaths 换成真实路径）；$TMPDIR 在 /var/folders 下
                return (Path("/tmp"), Path("/var/tmp"), Path("/var/folders"))
            case Category.DEVICES:
                return (Path("/dev"),)
            case Category.USER_CACHE:
                return (home / HOME_CACHE, home / "Library/Caches")
            case Category.PACKAGES:
                return (Path("/opt/homebrew"), *under(home, HOME_PACKAGES))
            case Category.TOOLCHAINS:
                # go 的用户配置目录（telemetry 计数，每次 go build 都写；#416 试跑）
                return (*under(home, HOME_TOOLCHAINS), home / "Library/Application Support/go")
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
        programs = " ".join(f"(literal {_quote(Path(p))})" for p in _UNSANDBOXED_LITERALS)
        lines = [
            "(version 1)",
            "(allow default)",
            f'(allow process-exec {programs} (regex #"{_UNSANDBOXED_REGEX}") (with no-sandbox))',
            f"{refuse})",
        ]
        for allowed, path in writable.rules:
            matcher = f"(subpath {_quote(path)})"
            lines.append(f"(allow file-write* {matcher})" if allowed else f"{refuse} {matcher})")
        return "\n".join(lines)

    def wrap(self, argv: Sequence[str], writable: WritablePaths) -> list[str]:
        return [os.fspath(SANDBOX_EXEC), "-p", self.profile(writable), *argv]
