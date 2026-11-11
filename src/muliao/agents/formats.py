"""各种资源文件的格式模块（memory_file、skill_file、mcp_config、secrets_format）交给 KindSpec 的两个槽位的约定
（agents.kinds）：读树按 load 读一项，写入口按 vet 校验要写成的全文。这里只有两个槽位的输入与输出，不依赖任何种类。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Loaded[C]:
    """load 读出的一项：内容、修改时间，以及照常加载但要报出的问题（记忆超长）。"""

    content: C
    updated_at: datetime
    warning: str | None = None


@dataclass(frozen=True)
class Draft:
    """写入口交给 vet 的一份文件：属于哪一项资源、要写成什么。"""

    # 这一项资源的文件（skill 是它的目录）
    item: Path
    name: str
    # skill 目录里的哪个文件（相对路径各段）；别的种类为空
    inner: tuple[str, ...]
    text: str
    memory_max_chars: int


def modified(path: Path) -> datetime:
    """文件的修改时间（UTC）。"""
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)
