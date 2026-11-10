"""记忆文件（`platform/memory/<名>.md`、`<agent 名>/memory.md`）的格式：读出的正文、存下的样子、长度上限。读树（agents.assets）
与写入口（agents.asset_files.vet，界面和文件工具都经它）只问这里。"""

from __future__ import annotations

from pathlib import Path

from muliao.errors import InvalidInput


def text_of(path: Path) -> str:
    """文件里的正文：去掉首尾空白（指令里显示的、版本按它算的都是它）。"""
    return path.read_text(encoding="utf-8").strip()


def over_limit(text: str, limit: int) -> str | None:
    """正文超过上限时的问题说明，否则 None。"""
    if len(text) <= limit:
        return None
    return f"memory is {len(text)} chars, over the limit of {limit}; condense it"


def stored(text: str, old: str | None, limit: int) -> str:
    """要写成 text 时文件里实际存下的全文：规整空白（tidy）后加结尾换行。规整后超过上限且比原来（old，新建为 None）长才
    拒绝（InvalidInput）：已经超长的（shell 追加、上限调低）可以一段段删短，不必一次删够。"""
    content = tidy(text)
    previous = len(tidy(old)) if old is not None else 0
    if len(content) > previous and (problem := over_limit(content, limit)):
        raise InvalidInput(f"after tidying whitespace the {problem}")
    return content + "\n" if content else ""


def tidy(content: str) -> str:
    """每行去掉行尾空白，连续空行合并成一个，去掉首尾空行，不留结尾换行：存下的就是指令里显示的样子。只动空白：记忆
    一条一行，空行个数和行尾空格没有意义，而指令里看不出它们（#361）。"""
    lines: list[str] = []
    for line in content.splitlines():
        line = line.rstrip()
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
