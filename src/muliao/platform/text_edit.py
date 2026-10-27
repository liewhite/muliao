"""文本的唯一规则（纯函数）：修改文本——替换恰好出现一次的片段（Replace），或追加一段（记忆、skill 正文、工作区文件都
用它）；截断过长的文本（交给模型、展示、通知的长输出都用它）。edit_file 的多处修改（按行号区间、按语法结构、换算原文
行号）只有工作区工具用，在 workspace.file_edit。"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from muliao.errors import InvalidInput


def replace_once(current: str, old: str, new: str, what: str) -> str:
    """把恰好出现一次的 old 替换为 new；new 为空即删除。"""
    return splice(current, Replace(old, new).locate(current, what)[0])


class Splice(NamedTuple):
    """把文本的 [start, end) 字符换成 new。"""

    start: int
    end: int
    new: str


def splice(text: str, places: Sequence[Splice]) -> str:
    """按顺序排列、互不重叠的各处替换做出新文本。"""
    parts, at = [], 0
    for start, end, new in places:
        parts += [text[at:start], new]
        at = end
    return "".join([*parts, text[at:]])


@dataclass(frozen=True)
class Replace:
    """按原文定位：old 恰好出现一次（every 时每一处）替换为 new。也是 edit_file 的一种修改（workspace.file_edit.Edit）：
    origin 是那边的原文换算，按原文定位用不到。"""

    old: str
    new: str
    every: bool = False

    def locate(self, current: str, what: str, origin: object = None) -> tuple[list[Splice], int]:
        if self.every and not self.old:
            raise InvalidInput("old text must not be empty")
        at = [m.start() for m in re.finditer(re.escape(self.old), current)] if self.old else []
        if self.every and not at:
            raise InvalidInput(_miss(current, self.old, 0, f"old text must occur in {what}"))
        if not self.every and len(at) != 1:
            raise InvalidInput(
                _miss(current, self.old, len(at), f"old text must occur exactly once in {what}")
            )
        return [Splice(i, i + len(self.old), self.new) for i in at], len(at)


def _miss(current: str, old: str, count: int, rule: str) -> str:
    """替换失败的说明，带上能让模型改对的线索：多处时在哪几行，没有时最接近的是哪里。"""
    message = f"{rule}, found {count} occurrences"
    if not old:
        return message
    if count > 1:
        lines = sorted({current.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(old), current)})
        return f"{message} (at lines {listed(lines)}); include more surrounding text to make it unique"
    wanted = [line.strip() for line in old.splitlines()]
    have = [line.strip() for line in current.splitlines()]
    n = len(wanted)
    for i in range(len(have) - n + 1):
        if have[i : i + n] == wanted:
            return (
                f"{message}; lines {i + 1}-{i + n} match it except for whitespace/indentation: "
                "copy that text exactly as it is now"
            )
    first = next((w for w in wanted if w), "")
    at = [str(i + 1) for i, line in enumerate(have) if first and line == first]
    if at:
        return (
            f"{message}; its first non-blank line is at line {', '.join(at[:10])} but what follows differs: "
            "re-read that part and copy it exactly"
        )
    return f"{message}; not even its first non-blank line occurs: re-read the current text before editing"


def edit(current: str, old: str, new: str, what: str) -> str:
    """old 恰好出现一次时替换为 new（new 为空即删除）；old 为空时把 new 追加到末尾。"""
    return replace_once(current, old, new, what) if old else append(current, new)


def append(current: str, entry: str) -> str:
    """在末尾追加一段，与已有内容之间空一行。"""
    entry = entry.strip()
    if not entry:
        raise InvalidInput("nothing to append: new_text is empty (give old_text too to replace or delete)")
    return f"{current.rstrip()}\n\n{entry}\n" if current.strip() else f"{entry}\n"


def listed(line_numbers: list[int]) -> str:
    """报错里「在哪几行」的列表，最多列 20 个。"""
    return ", ".join(map(str, line_numbers[:20])) + (" …" if len(line_numbers) > 20 else "")


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated: {len(text) - limit} more chars]"
