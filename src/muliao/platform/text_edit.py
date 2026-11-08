"""文本的唯一规则（纯函数）：修改文本——替换恰好出现一次的片段（locate_once、replace_once），或追加一段（记忆、
skill 正文、工作区文件都用它），依次做多处这样的修改（edit_each）；截断过长的文本（交给模型、展示、通知的长输出都用它）；带行号展示的 cat -n 格式（numbered）。edit_file 的多处修改（替换全部、按行号
区间、按语法结构、换算原文行号）只有工作区工具用，在 workspace.file_edit。"""

import re
from collections.abc import Sequence
from typing import NamedTuple

from muliao.errors import InvalidInput


def occurrences(current: str, old: str) -> list[int]:
    """old 在 current 里每一处的起点（不重叠）；old 为空时没有。"""
    return [m.start() for m in re.finditer(re.escape(old), current)] if old else []


def locate_once(current: str, old: str, what: str) -> int:
    """old 恰好出现一次时它的起点；否则 InvalidInput，带上能改对的线索。"""
    at = occurrences(current, old)
    if len(at) != 1:
        raise InvalidInput(miss(current, old, len(at), f"old text must occur exactly once in {what}"))
    return at[0]


def replace_once(current: str, old: str, new: str, what: str) -> str:
    """把恰好出现一次的 old 替换为 new；new 为空即删除。"""
    at = locate_once(current, old, what)
    return splice(current, [Splice(at, at + len(old), new)])


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


def miss(current: str, old: str, count: int, rule: str) -> str:
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


class TextChange(NamedTuple):
    """一处修改，规则见 edit：old 为空是追加。"""

    old: str
    new: str


class ChangeFailed(InvalidInput):
    """edit_each 里第 index 处（1 起）做不了；change 是那一处（调用方据此决定要不要在报错里附上原文）。index > 1 时
    报错里的行号线索指前面几处改过之后的文本。"""

    def __init__(self, message: str, change: TextChange, index: int) -> None:
        super().__init__(message)
        self.change = change
        self.index = index


NO_EDITS = "no edits given"


def failed_edit(i: int, n: int, error: Exception, after: str = "") -> str:
    """多处修改里第 i 处（共 n 处）失败的说明，edit_file 与 memory_edit / edit_skill 共用；after 补充这一处是在什么文本上
    找的。"""
    return f"edit {i} of {n}{after}: {error}; nothing was written"


def edit_each(current: str, changes: Sequence[TextChange], what: str) -> str:
    """依次做一段文本的多处修改（memory_edit 的 `edits`；edit_skill 跨文件，在 skills._edited 按同样的规则做），后一处在前面改过的结果上找原文。任何一处失败都整体失败，
    报错指出是第几处；调用方只在成功后写入，所以不会留下改了一半的文本。"""
    if not changes:
        raise InvalidInput(NO_EDITS)
    text = current
    for i, change in enumerate(changes, 1):
        try:
            text = edit(text, change.old, change.new, what)
        except InvalidInput as e:
            if len(changes) == 1:
                raise ChangeFailed(str(e), change, i) from e
            after = (
                f" (old_text is matched against the text with the {i - 1} earlier edit(s) applied)"
                if i > 1
                else ""
            )
            raise ChangeFailed(failed_edit(i, len(changes), e, after), change, i) from e
    return text


def append(current: str, entry: str) -> str:
    """在末尾追加一段，与已有内容之间空一行。"""
    entry = entry.strip()
    if not entry:
        raise InvalidInput("nothing to append: new_text is empty (give old_text too to replace or delete)")
    return f"{current.rstrip()}\n\n{entry}\n" if current.strip() else f"{entry}\n"


def numbered(lines: Sequence[str], first: int = 1) -> str:
    """按 `cat -n` 的格式编号：行号（第一行为 first）右对齐占 6 列，后跟一个制表符。带行号的内容（read_file、edit_file 的回显与报错、
    memory_edit 报错里的全文）都用它，模型拿来就认得。"""
    return "\n".join(f"{i:6}\t{line}" for i, line in enumerate(lines, first))


def listed(line_numbers: list[int]) -> str:
    """报错里「在哪几行」的列表，最多列 20 个。"""
    return ", ".join(map(str, line_numbers[:20])) + (" …" if len(line_numbers) > 20 else "")


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated: {len(text) - limit} more chars]"
