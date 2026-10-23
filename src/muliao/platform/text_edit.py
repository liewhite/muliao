"""文本的唯一规则（纯函数）：修改文本——替换恰好出现一次的片段，或追加一段（记忆、skill 正文、工作区文件都用它）；
工作区文件的多处修改（Edit：按原文、按行号区间，按语法结构的在 platform.syntax_edit）；带行号展示；截断过长的文本
（交给模型、展示、通知的长输出都用它）。"""

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from muliao.errors import InvalidInput

# 带行号展示时单行的上限：一行压缩过的 JS/JSON 能吃掉整个输出预算
MAX_LINE_CHARS = 2000


def replace_once(current: str, old: str, new: str, what: str) -> str:
    """把恰好出现一次的 old 替换为 new；new 为空即删除。"""
    count = current.count(old) if old else 0
    if count != 1:
        raise InvalidInput(_miss(current, old, count, f"old text must occur exactly once in {what}"))
    return current.replace(old, new)


def replace_all(current: str, old: str, new: str, what: str) -> tuple[str, int]:
    """把 old 的每一处都替换为 new，返回新文本与替换数；一处都没有是错误。"""
    if not old:
        raise InvalidInput("old text must not be empty")
    count = current.count(old)
    if not count:
        raise InvalidInput(_miss(current, old, 0, f"old text must occur in {what}"))
    return current.replace(old, new), count


class Edit(Protocol):
    """一处修改：按自己的方式定位要改的地方并替换，返回新文本与替换数；定位不到就抛 InvalidInput。"""

    def apply(self, current: str, what: str) -> tuple[str, int]: ...


@dataclass(frozen=True)
class Replace:
    """按原文定位：old 恰好出现一次（every 时每一处）替换为 new。"""

    old: str
    new: str
    every: bool = False

    def apply(self, current: str, what: str) -> tuple[str, int]:
        if self.every:
            return replace_all(current, self.old, self.new, what)
        return replace_once(current, self.old, self.new, what), 1


@dataclass(frozen=True)
class LineRange:
    """按行号定位：第 start 到 end 行（1 起算、含两端）整行替换为 new。anchor 是第 start 行的原文（两端空白不计），
    对不上说明行号已过期（文件变了，或前面的修改挪动了行），报错并展示那几行现在的内容，不改错地方。"""

    start: int
    end: int
    anchor: str
    new: str

    def apply(self, current: str, what: str) -> tuple[str, int]:
        lines = current.splitlines(keepends=True)
        if not 1 <= self.start <= self.end:
            raise InvalidInput(
                f"need 1 <= start_line <= end_line, got start_line={self.start} end_line={self.end}"
            )
        if self.end > len(lines):
            raise InvalidInput(f"end_line {self.end} is past the end: {what} has {len(lines)} lines")
        if "\n" in self.anchor.strip():
            raise InvalidInput("anchor must be the text of the single line start_line, not several lines")
        line, anchor = lines[self.start - 1].rstrip("\r\n"), self.anchor.strip()
        # 超长的行在 read_file 里是截断着展示的，照抄截断后的样子也算对上
        if anchor not in {line.strip(), _clip(line).strip()}:
            plain = current.splitlines()
            first, last = max(self.start - 3, 0), min(self.end + 2, len(plain))
            raise InvalidInput(
                f"anchor does not match line {self.start} of {what}; lines {first + 1}-{last} are now:\n"
                f"{numbered(plain, first, last)}\ngive the line numbers and anchor of the text as it is now"
            )
        replaced = "".join(lines[self.start - 1 : self.end])
        head, tail = "".join(lines[: self.start - 1]), "".join(lines[self.end :])
        return head + as_lines(self.new, replaced) + tail, 1


def as_lines(new: str, replaced: str) -> str:
    """替换整行时的新内容：被替换的行以换行结尾而 new 没有时补上，new 末尾的换行因此可写可不写；new 为空即删掉这些行。"""
    if new and replaced.endswith("\n") and not new.endswith("\n"):
        return new + "\n"
    return new


def apply_each(current: str, edits: Sequence[Edit], what: str) -> tuple[str, int]:
    """依次做多处修改（后一项作用在前面改过的文本上），返回新文本与总替换数。
    任何一项失败都整体失败，报错指出是第几项；调用方只在成功后写盘，所以不会留下改了一半的文件。"""
    if not edits:
        raise InvalidInput("no edits given")
    total = 0
    for i, change in enumerate(edits, 1):
        try:
            current, count = change.apply(current, what)
        except InvalidInput as e:
            if len(edits) == 1:
                raise
            after = f" (checked against the text with the {i - 1} earlier edit(s) applied)" if i > 1 else ""
            raise InvalidInput(f"edit {i} of {len(edits)}{after}: {e}; nothing was written") from e
        total += count
    return current, total


def changed_ranges(before: str, after: str, context: int) -> list[tuple[int, int]]:
    """after 里改动过的行（各带前后 context 行，相邻的合并）的 [起, 止) 行号范围，0 起算。按前后文本比对得出，
    与替换的先后次序无关（后一处替换可能挪动前一处的位置）。纯删除的地方只剩上下文行；整个文件被删空时为空列表。"""
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    ranges = []
    for group in matcher.get_grouped_opcodes(context):
        start, end = group[0][3], group[-1][4]
        if start < end:
            ranges.append((start, end))
    return ranges


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
        raise InvalidInput("nothing to append")
    return f"{current.rstrip()}\n\n{entry}\n" if current.strip() else f"{entry}\n"


def listed(line_numbers: list[int]) -> str:
    """报错里「在哪几行」的列表，最多列 20 个。"""
    return ", ".join(map(str, line_numbers[:20])) + (" …" if len(line_numbers) > 20 else "")


def numbered(lines: list[str], start: int, end: int) -> str:
    """lines[start:end]，每行前加 1 起算的行号和制表符（read_file、edit_file 的回显与报错同一格式），过长的行截断。"""
    return "\n".join(f"{i + 1}\t{_clip(lines[i])}" for i in range(start, end))


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else f"{line[:MAX_LINE_CHARS]}…[line truncated]"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated {len(text) - limit} chars]"
