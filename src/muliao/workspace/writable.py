"""agent 能写哪里（#416）：命令（run_shell、后台任务、触发器的前置检查与 stream）与文件工具只能写可写路径，防 agent 误删、
误写别处的东西。对 agent 无感：工具、说明、提示词都不提它；在范围内一切照旧，写到范围外时只在原本的系统报错后面补一句
说明（denial_note）。

可写路径 = 它的工作目录 + 默认可写的几类路径（DEFAULT_WRITABLE）− 受保护的（PROTECTED）+ 它的 `write:path:<目录>` 授权
− 它的 deny 里的 `write:path:<目录>`，后面的覆盖前面的。类别换成具体路径、命令怎么关进沙箱由执行层按当前系统做
（platform.sandbox）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio

from muliao.agents.api import Rules
from muliao.errors import Forbidden
from muliao.platform.sandbox import Category, Confinement, Sandbox, WritablePaths

log = logging.getLogger(__name__)

# 每个 agent 都能写的几类路径
DEFAULT_WRITABLE = (
    Category.TEMP,
    Category.DEVICES,
    Category.USER_CACHE,
    Category.PACKAGES,
    Category.TOOLCHAINS,
    Category.TOOL_STATE,
)
# 落在默认可写范围里、但不让写的（平台自身的安装在 PACKAGES 的 ~/.local 下）；要写须单独授权
PROTECTED = (Category.PLATFORM_INSTALL,)


def _note(path: Path) -> str:
    return (
        f"[{path} is outside the paths this agent may write (its workspace, temp, cache and toolchain "
        "directories, plus directories granted to it), so the write was refused. If it really needs to write "
        "there, ask the user or an agent with the admin role to grant it 'write:path:<directory>'.]"
    )


class Confiner:
    """按 agent 现在的分发规则算出它的 Confinement：每次起命令、写文件时现算，改了授权下一条命令就生效。"""

    def __init__(
        self,
        sandbox: Sandbox,
        workspace_of: Callable[[str], Path],
        rules_of: Callable[[str], Awaitable[Rules]],
        home: Path | None = None,
    ):
        self._sandbox = sandbox
        self._workspace_of = workspace_of
        self._rules_of = rules_of
        self._home = Path.home() if home is None else home

    async def of(self, agent_id: str) -> Confinement:
        allow, deny = (await self._rules_of(agent_id)).paths()
        rules = [
            (True, self._workspace_of(agent_id)),
            *((True, p) for c in DEFAULT_WRITABLE for p in self._sandbox.paths(c)),
            *((False, p) for c in PROTECTED for p in self._sandbox.paths(c)),
            *((True, self._expand(p)) for p in allow),
            *((False, self._expand(p)) for p in deny),
        ]
        # 换成真实路径要读文件系统
        writable = await anyio.to_thread.run_sync(WritablePaths.of, rules, agent_id)
        return Confinement(self._sandbox, writable)

    def _expand(self, path: str) -> Path:
        # 规则的形式已在写入时校验（agents.permissions.kind_of）：绝对路径或 ~/ 开头
        return self._home / path.removeprefix("~/") if path.startswith("~/") else Path(path)


def check_write(confinement: Confinement, path: Path) -> None:
    """文件工具写 path 之前：不在可写范围内时 Forbidden，说法与命令里 Python 被系统拒绝时一样，后面补同一句说明。只记录
    不拦的模式下记一条日志放行。"""
    if confinement.writable.permits(path):
        return
    if confinement.enforcing:
        raise Forbidden(f"[Errno 1] Operation not permitted: '{path}'\n{_note(path)}")
    log.warning(
        "write outside writable paths (report mode)",
        extra={"agent": confinement.writable.owner, "path": str(path)},
    )


# 系统拒绝写时的报错（EPERM、EACCES 的标准说法），与其中的路径：绝对路径或 ~/ 开头，到空白、引号或冒号为止
_REFUSED = ("Operation not permitted", "Permission denied")
_PATH = re.compile(r"(?<![\w.~/-])(~/[^\s'\":]*|/[^\s'\":]+)")


def denial_note(confinement: Confinement, output: str, home: Path | None = None) -> str | None:
    """命令输出里有被拒绝的写、且报错里的路径在可写范围外时补的说明；不是沙箱拦的（如文件本身只读）不补。
    只认绝对路径与 ~/ 开头的路径：相对路径不知道命令当时在哪个目录。"""
    if not confinement.enforcing:
        return None
    home = Path.home() if home is None else home
    for line in output.splitlines():
        if not any(r in line for r in _REFUSED):
            continue
        for found in _PATH.findall(line):
            path = home / found.removeprefix("~/") if found.startswith("~/") else Path(found)
            if not confinement.writable.permits(path):
                return _note(path)
    return None
