"""agent 能写哪里（#416）：命令（run_shell、后台任务、触发器的前置检查与 stream）与文件工具只能写可写路径，防 agent 误删、
误写别处的东西。对 agent 无感：工具、说明、提示词都不提它；在范围内一切照旧，写到范围外时只在原本的系统报错后面补一句
说明（denial_note）。

可写路径 = 它的工作目录 + 默认可写的几类路径（DEFAULT_WRITABLE）− 受保护的（PROTECTED）+ 它的 `write:path:<目录>` 授权
− 它的 deny 里的 `write:path:<目录>` − 整棵 AI 资源树 + 树里它能改的资源（agents.access.write_scope：自己的目录、角色
授予 write 的、admin 整棵树，#499）− 平台的私有文件（非 admin），后面的覆盖前面的。「能不能改某项资源」由此完全由沙箱回答。
类别换成具体路径、命令怎么关进沙箱由执行层按当前系统做（platform.sandbox）。

读只拦一处（#539）：平台的私有文件（config.Settings.private_files：配置、备份、推送私钥）只有 admin 能读，命令由沙箱拒绝、
read_file 由 check_read 拒绝，补的说明同样走 Confinement.explain。防误操作（把密钥打进会话记录），不防故意。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio

from muliao.agents.api import WriteScope
from muliao.errors import Forbidden
from muliao.platform.sandbox import Category, Confinement, Sandbox, WritablePaths, covers, expand, real_forms

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
    # 读受系统保护的目录也报同样的错，分不出来：只说「如果这是一次写」
    return (
        f"[If this was a write: {path} is outside the paths this agent may write (its workspace, temp, cache "
        "and toolchain directories, plus directories granted to it), so the write sandbox refused it. If it "
        "really needs to write there, ask the user or an agent with the admin role to grant it "
        "'write:path:<directory>'.]"
    )


def _private_note(path: Path) -> str:
    return (
        f"[{path} is one of the platform's private files (its config with the login password and keys, its "
        "backups, its push key): only an agent with the admin role may read or write them, so the sandbox "
        "refused it. If you need something from them, ask the user or an admin agent.]"
    )


class Confiner:
    """按 agent 现在的分发规则与资源归属算出它的 Confinement：每次起命令、写文件时现算，改了授权下一条命令就生效。
    受保护的排在授权之前：`write:path` 授权它本身或它的上级目录都会放开它；资源树排在授权之后，只按归属。"""

    def __init__(
        self,
        sandbox: Sandbox,
        workspace_of: Callable[[str], Path],
        scope_of: Callable[[str], Awaitable[WriteScope]],
        # 服务运行用户的家目录，与适配器的是同一个（组合根注入）
        home: Path,
        # 平台的私有文件（config.Settings.private_files）：只有 admin 能读写
        private: tuple[Path, ...],
    ):
        self._sandbox = sandbox
        self._workspace_of = workspace_of
        self._scope_of = scope_of
        self._home = home
        self._private = private

    async def of(self, agent_id: str) -> Confinement:
        scope = await self._scope_of(agent_id)
        private = () if scope.admin else self._private
        # 规则的形式已在读入时校验（agents.permissions.kind_of）：绝对路径或 ~/ 开头
        rules = [
            (True, self._workspace_of(agent_id)),
            *((True, p) for c in DEFAULT_WRITABLE for p in self._sandbox.paths(c)),
            *((False, p) for c in PROTECTED for p in self._sandbox.paths(c)),
            *((True, expand(p, self._home)) for p in scope.granted),
            *((False, expand(p, self._home)) for p in scope.revoked),
            # 资源树只按归属管：排在 write:path 之后，祖先目录的授权（如 ~/.muliao）不放开 access.toml
            (False, scope.tree),
            *((True, p) for p in scope.assets),
            # 平台的私有文件排在最后：任何授权都放不开
            *((False, p) for p in private),
        ]
        # 换成真实路径要读文件系统
        writable, unreadable = await anyio.to_thread.run_sync(
            lambda: (WritablePaths.of(rules, agent_id), tuple(f for p in private for f in real_forms(p)))
        )

        def explain(path: Path) -> str:
            if covers(unreadable, path):
                return _private_note(path)
            return scope.refusal(path) or _note(path)

        return Confinement(self._sandbox, writable, self._home, explain, unreadable)


def check_write(confinement: Confinement, path: Path) -> None:
    """文件工具写 path 之前：不在可写范围内时 Forbidden，说法与命令里 Python 被系统拒绝时一样，后面补同一句说明。只记录
    不拦的模式下记一条日志放行。"""
    if confinement.writable.permits(path):
        return
    if confinement.enforcing:
        raise Forbidden(f"[Errno 1] Operation not permitted: '{path}'\n{confinement.explain(path)}")
    log.warning(
        "write outside writable paths (report mode)",
        extra={"agent": confinement.writable.owner, "path": str(path)},
    )


def check_read(confinement: Confinement, path: Path) -> None:
    """read_file 读 path 之前：是它读不了的（平台的私有文件）时 Forbidden，说法与命令里被沙箱拒绝时一样。只记录不拦的
    模式下记一条日志放行。"""
    if confinement.readable(path):
        return
    if confinement.enforcing:
        raise Forbidden(f"[Errno 1] Operation not permitted: '{path}'\n{confinement.explain(path)}")
    log.warning(
        "read of a private file (report mode)",
        extra={"agent": confinement.writable.owner, "path": str(path)},
    )


# 写沙箱拒绝时系统报的错一律是 EPERM：各语言的说法大小写不一（Node `EPERM: operation not permitted`、Go
# `operation not permitted`）。EACCES（Permission denied）不是它拦的，不认
_REFUSED = re.compile(r"operation not permitted", re.IGNORECASE)
# 报错里的路径：绝对路径或 ~/ 开头，到空白、引号或冒号为止（带空格的路径只取到空格前，可能拿错：只影响补不补说明）
_PATH = re.compile(r"(?<![\w.~/-])(~/[^\s'\":]*|/[^\s'\":]+)")


def denial_note(confinement: Confinement, output: str) -> str | None:
    """命令输出里有被拒绝的操作、且报错里的路径在可写范围外时补的说明；范围内的拒绝不是写沙箱拦的，不补。
    只认绝对路径与 ~/ 开头的路径：相对路径不知道命令当时在哪个目录。"""
    if not confinement.enforcing:
        return None
    for line in output.splitlines():
        if not _REFUSED.search(line):
            continue
        for found in _PATH.findall(line):
            path = expand(found, confinement.home)
            if not confinement.writable.permits(path):
                return confinement.explain(path)
    return None
