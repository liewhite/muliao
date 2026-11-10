"""agent 自定义页面（#355）：agent 在工作目录 `site/<页面名>/` 下写的网页（监控、数据展示），界面列出并打开。

约定只有目录：`site/<页面名>/index.html` 是页面入口，同目录的其他文件（脚本、样式、数据 JSON）随页面一起托管；
标题取 index.html 的 `<title>`，没有就用页面名。页面的数据由 agent 用现有手段刷新（触发器、常驻脚本写 data.json），
页面自己轮询，平台不运行 agent 的服务。

路径规则同工作目录浏览（browse.py）：解析符号链接后必须仍在页面目录内，只防误走出去，不是安全隔离。页面内容是
agent 写的，HTTP 层按不可信内容托管（沙箱、不同源，见 api/sites.py）。
"""

import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from muliao.errors import NotFound
from muliao.platform.fs import real, resolve_within
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


def _title_of(index: Path, fallback: str) -> str:
    with index.open("rb") as f:
        head = f.read(_TITLE_SCAN_BYTES)
    m = _TITLE.search(head)
    title = html.unescape(m.group(1).decode("utf-8", "replace")).strip() if m else ""
    return " ".join(title.split()) or fallback


def _page_of(site: Path, name: str) -> tuple[Path, Path] | None:
    """site 下名为 name 的页面：(页面目录, index.html)，都已解析符号链接；页面目录必须在 site 内、index.html 必须在
    页面目录内且是文件，否则不是页面。"""
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        return None
    page_dir = real(site / name)
    if page_dir == site or not page_dir.is_relative_to(site):
        return None
    index = real(page_dir / INDEX)
    if not index.is_relative_to(page_dir) or not index.is_file():
        return None
    return page_dir, index


class Sites:
    def __init__(self, workspace_of: Callable[[str], Path]):
        self._workspace_of = workspace_of

    def _site(self, agent_id: str) -> Path | None:
        """site 目录（已解析符号链接）；没有或不在工作目录内为 None。"""
        root = real(self._workspace_of(agent_id))
        site = real(root / SITE_DIR)
        return site if site.is_relative_to(root) and site.is_dir() else None

    def pages(self, agent_id: str) -> list[SitePage]:
        """agent 的全部页面，按名称排序：site 下每个含 index.html 的子目录。没有 site 目录就是没有页面；
        与工作目录浏览一样，读不了（stat、读标题时出错，如正被删除）的不列出。"""
        site = self._site(agent_id)
        if site is None:
            return []
        pages = []
        for d in site.iterdir():
            try:
                found = _page_of(site, d.name)
                if found is None:
                    continue
                page_dir, index = found
                modified = max(
                    [index.stat().st_mtime, *(f.stat().st_mtime for f in page_dir.iterdir() if f.is_file())]
                )
                pages.append(
                    SitePage(
                        name=d.name,
                        title=_title_of(index, d.name),
                        modified=datetime.fromtimestamp(modified, UTC),
                    )
                )
            except OSError:
                continue
        return sorted(pages, key=lambda p: p.name)

    def file(self, agent_id: str, page: str, path: str) -> WorkspaceFile:
        """页面目录里的一个文件；path 为空或以 / 结尾时取那一层的 index.html（不带 / 的目录不补：页面里的相对路径
        会解析到上一级）。"""
        site = self._site(agent_id)
        found = None if site is None else _page_of(site, page)
        if found is None:
            raise NotFound(f"agent {agent_id!r} has no page {page!r}")
        page_dir, _ = found
        target = resolve_within(page_dir, path + INDEX if path == "" or path.endswith("/") else path)
        if not target.is_file():
            raise NotFound(f"no such file in page {page!r}: {path!r}")
        return WorkspaceFile(target, media_type_of(target.name))
