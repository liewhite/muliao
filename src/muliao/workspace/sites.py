"""agent 自定义页面（#355）：agent 在工作目录 `site/<页面名>/` 下写的网页（监控、数据展示），界面列出并打开。

约定只有目录：`site/<页面名>/index.html` 是页面入口，同目录的其他文件（脚本、样式、数据 JSON）随页面一起托管；
标题取 index.html 的 `<title>`，没有就用页面名。页面的数据由 agent 用现有手段刷新（触发器、常驻脚本写 data.json），
页面自己轮询，平台不运行 agent 的服务。

路径规则同工作目录浏览（browse.py）：解析符号链接后必须仍在页面目录内，只防误走出去，不是安全隔离。页面内容是
agent 写的，HTTP 层按不可信内容托管（沙箱、不同源，见 api/sites.py）。
"""

import html
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from muliao.errors import InvalidInput, NotFound
from muliao.workspace.browse import WorkspaceFile, media_type_of

SITE_DIR = "site"
INDEX = "index.html"
# 只在 index.html 开头找 <title>：标题总在 <head> 里
_TITLE_SCAN_BYTES = 64 * 1024
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class SitePage:
    name: str
    title: str
    # 页面目录里（不含子目录）最近改动的文件的时间：数据文件刷新也算
    modified: datetime


def _real(p: Path) -> Path:
    return Path(os.path.realpath(p))


def _title_of(index: Path, fallback: str) -> str:
    with index.open("rb") as f:
        head = f.read(_TITLE_SCAN_BYTES)
    m = _TITLE.search(head)
    title = html.unescape(m.group(1).decode("utf-8", "replace")).strip() if m else ""
    return " ".join(title.split()) or fallback


class Sites:
    def __init__(self, workspace_of: Callable[[str], Path]):
        self._workspace_of = workspace_of

    def _site_root(self, agent_id: str) -> tuple[Path, Path]:
        """(工作目录, site 目录)，都已解析符号链接；site 目录可能不存在。"""
        root = _real(self._workspace_of(agent_id))
        return root, _real(root / SITE_DIR)

    def pages(self, agent_id: str) -> list[SitePage]:
        """agent 的全部页面，按名称排序：site 下每个含 index.html 的子目录。没有 site 目录就是没有页面；
        指向工作目录外的符号链接不算。"""
        root, site = self._site_root(agent_id)
        if not site.is_relative_to(root) or not site.is_dir():
            return []
        pages = []
        for d in site.iterdir():
            real = _real(d)
            index = real / INDEX
            if not real.is_relative_to(root) or not index.is_file():
                continue
            mtimes = []
            for f in real.iterdir():
                try:
                    if f.is_file():
                        mtimes.append(f.stat().st_mtime)
                except OSError:
                    continue
            pages.append(
                SitePage(
                    name=d.name,
                    title=_title_of(index, d.name),
                    modified=datetime.fromtimestamp(max(mtimes), UTC),
                )
            )
        return sorted(pages, key=lambda p: p.name)

    def file(self, agent_id: str, page: str, path: str) -> WorkspaceFile:
        """页面目录里的一个文件；path 为空或指向目录时取其中的 index.html。"""
        if "\0" in page or "\0" in path:
            raise InvalidInput("path contains a NUL character")
        if page in ("", ".", "..") or "/" in page:
            raise InvalidInput(f"not a page name: {page!r}")
        rel = PurePosixPath(path)
        if rel.is_absolute():
            raise InvalidInput(f"path must be relative to the page: {path!r}")
        root, site = self._site_root(agent_id)
        page_dir = _real(site / page)
        if not page_dir.is_relative_to(root) or not (page_dir / INDEX).is_file():
            raise NotFound(f"agent {agent_id!r} has no page {page!r}")
        target = _real(page_dir / rel)
        if not target.is_relative_to(page_dir):
            raise InvalidInput(f"path is outside the page: {path!r}")
        if target.is_dir():
            target = target / INDEX
        if not target.is_file():
            raise NotFound(f"no such file in page {page!r}: {path!r}")
        return WorkspaceFile(target, media_type_of(target.name))
