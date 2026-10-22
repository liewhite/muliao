"""文本的唯一规则（纯函数）：修改文本——替换恰好出现一次的片段，或追加一段（记忆、指令、skill 正文、工作区文件都用它）；
截断过长的文本（交给模型、展示、通知的长输出都用它）。"""

import difflib
import re

from muliao.errors import InvalidInput


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


def replace_each(current: str, edits: list[tuple[str, str, bool]], what: str) -> tuple[str, int]:
    """依次做多处替换（每项 (old, new, 是否替换全部)，后一项作用在前面替换后的文本上），返回新文本与总替换数。
    任何一项失败都整体失败，报错指出是第几项；调用方只在成功后写盘，所以不会留下改了一半的文件。"""
    if not edits:
        raise InvalidInput("no edits given")
    total = 0
    for i, (old, new, every) in enumerate(edits, 1):
        try:
            if every:
                current, count = replace_all(current, old, new, what)
            else:
                current, count = replace_once(current, old, new, what), 1
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
        shown = ", ".join(map(str, lines[:20])) + (" …" if len(lines) > 20 else "")
        return f"{message} (at lines {shown}); include more surrounding text to make it unique"
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


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated {len(text) - limit} chars]"
