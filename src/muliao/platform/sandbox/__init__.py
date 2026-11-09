"""写沙箱的执行层（#416）：agent 的命令只能写给它的可写路径，别处的写被操作系统拒绝。防 agent 误删、误写别处的东西，
不防故意绕过（见架构文档「安全边界」）。

与操作系统无关的部分在这里：可写路径（WritablePaths）、路径的类别（Category）、适配器的接口（Sandbox）与按当前系统挑
适配器（sandbox_for）。每个系统一个适配器文件，把类别换成本系统的具体路径、把命令包进本系统的沙箱；当前系统没有适配器
时报错，不退回到不隔离。哪些类别默认可写、agent 的授权怎么并进来，是领域的事（workspace.writable）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

# 越界的写：enforce 拒绝；report 放行但由适配器记进系统日志（上线前试跑，统计漏掉的路径）
Mode = Literal["enforce", "report"]


class Category(StrEnum):
    """一类路径：领域按类别声明默认可写与受保护的范围，适配器换成本系统的具体路径。"""

    # 临时目录（/tmp 与每个用户的临时目录）
    TEMP = "temp"
    # 设备文件（/dev/null、终端）
    DEVICES = "devices"
    # 用户缓存（XDG 缓存、系统的用户缓存目录）
    USER_CACHE = "user_cache"
    # 包管理器的安装目录与包缓存（Homebrew、~/.local、npm、Maven 等）
    PACKAGES = "packages"
    # 语言工具链（pyenv、nvm、cargo、rustup、go 等）
    TOOLCHAINS = "toolchains"
    # 工具运行时写的状态：写不了会报错或卡住、丢了也无害（known_hosts、docker / kube 配置缓存、REPL 历史）
    TOOL_STATE = "tool_state"
    # 平台自身的安装（运行中的服务的 Python 环境与启动命令）
    PLATFORM_INSTALL = "platform_install"


def expand(path: str, home: Path) -> Path:
    """`~/x` 换成 home 下的路径，其余原样（授权规则与报错里的路径共用）。"""
    return home / path.removeprefix("~/") if path.startswith("~/") else Path(path)


def real_forms(path: Path) -> tuple[Path, ...]:
    """路径在文件系统里的写法：上级目录解析符号链接后的它（链接本身），以及连它一起解析后的（链接指向的）。系统按真实路径
    判断，而 /tmp 这类上级是链接的路径、本身是链接的路径两种都会被写到。"""
    path = Path(os.path.abspath(path))
    forms = [Path(os.path.realpath(path.parent)) / path.name, Path(os.path.realpath(path))]
    return tuple(dict.fromkeys(forms))


@dataclass(frozen=True)
class WritablePaths:
    """一组可写路径：按顺序的 (可写, 路径)，路径都是真实路径（real_forms）。一个路径能不能写看最后一条覆盖它的规则（规则的
    路径是它本身或它的上级目录），没有覆盖它的就不能写。后面的规则覆盖前面的：先放开一类目录，再收回其中一部分。"""

    rules: tuple[tuple[bool, Path], ...]
    # 这组路径属于谁（agent 的 id）：只用于日志里认出是谁的命令
    owner: str

    @classmethod
    def of(cls, rules: Iterable[tuple[bool, Path]], owner: str) -> WritablePaths:
        """rules 的路径可以带符号链接：在这里换成真实路径（读文件系统）。"""
        return cls(tuple((w, form) for w, p in rules for form in real_forms(p)), owner)

    def permits(self, path: Path) -> bool:
        """path（绝对路径）能不能写：按它最终写到的真实路径判断。"""
        target = Path(os.path.realpath(path))
        verdict = False
        for writable, rule in self.rules:
            if target == rule or rule in target.parents:
                verdict = writable
        return verdict


class Sandbox(Protocol):
    """一个系统的适配器。"""

    mode: Mode

    @staticmethod
    def supported() -> bool:
        """本机能不能用这个适配器（sandbox_for 据此挑选）。"""
        ...

    def paths(self, category: Category) -> tuple[Path, ...]:
        """这一类在本系统的具体路径（不存在的也列出：以后建出来就能写）。"""
        ...

    def wrap(self, argv: Sequence[str], writable: WritablePaths) -> list[str]:
        """把命令包进沙箱：返回实际要执行的 argv，它与它起的所有子进程只能写 writable 允许的路径。"""
        ...


def sandbox_for(mode: Mode, home: Path, platform: str = sys.platform) -> Sandbox:
    """当前系统的适配器（home：服务运行用户的家目录，家目录下的类别按它算）；没有时 RuntimeError（启动时报出，
    不退回到不隔离）。"""
    if platform == "darwin":
        from muliao.platform.sandbox.seatbelt import Seatbelt

        if Seatbelt.supported():
            return Seatbelt(mode, home)
    raise RuntimeError(
        f"no write sandbox is available on this system ({platform}); agents' shell commands cannot run unconfined"
    )


@dataclass(frozen=True)
class Confinement:
    """一个 agent 的命令在什么限制下跑：本系统的适配器 + 它的可写路径。shell.spawn_bash 用它包命令，文件工具用它检查写。"""

    sandbox: Sandbox
    writable: WritablePaths
    # 家目录：规则与报错里的 ~/ 按它展开（expand）
    home: Path
    # 写到可写路径外的一个路径被拒时，补在系统报错后面的说明（领域给的：为什么不让写、找谁授权）
    explain: Callable[[Path], str]

    def wrap(self, argv: Sequence[str]) -> list[str]:
        return self.sandbox.wrap(argv, self.writable)

    @property
    def enforcing(self) -> bool:
        return self.sandbox.mode == "enforce"
