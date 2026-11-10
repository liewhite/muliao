"""各类路径里在类 Unix 系统上相同的部分（家目录下各工具约定的目录、平台自身的安装），由各系统的适配器并进自己的路径。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from muliao.platform.fs import real

# 家目录下的相对路径
HOME_PACKAGES = (
    ".local",
    ".npm",
    ".yarn",
    ".m2",
    ".gradle",
    ".ivy2",
    ".sbt",
)
HOME_TOOLCHAINS = (
    ".pyenv",
    ".nvm",
    ".cargo",
    ".rustup",
    "go",
    ".foundry",
    ".svm",
    ".sdkman",
)
HOME_TOOL_STATE = (
    ".ssh/known_hosts",
    ".docker",
    ".kube",
    ".aws/sso/cache",
    ".aws/cli/cache",
    ".ipython",
    ".matplotlib",
    # gcloud 刷新访问令牌时写它的凭据库（#416 试跑：sto-bard-deposit-monitor）
    ".config/gcloud",
    ".python_history",
    ".psql_history",
    ".node_repl_history",
    ".lesshst",
    ".viminfo",
)
HOME_CACHE = ".cache"


def under(home: Path, relative: tuple[str, ...]) -> tuple[Path, ...]:
    return tuple(home / r for r in relative)


def platform_install() -> tuple[Path, ...]:
    """运行中的服务的 Python 环境，以及 PATH 上指向它的 muliao 启动命令（uv tool 装在 ~/.local/bin 的链接）。"""
    env = real(sys.prefix)
    found = shutil.which("muliao")
    launcher = Path(found) if found is not None and env in real(found).parents else None
    return (env, *((launcher,) if launcher is not None else ()))
