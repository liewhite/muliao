"""edit_file 的多处修改（纯函数）：每处修改（Edit）按自己的方式定位——原文（Replace）、行号区间
（LineRange）、语法结构（workspace.syntax_edit.PatternEdit）——只报出要做的替换（Splice），apply_each 依次拼出新文本；
行号一律指调用前的原文，由 Origin 换算。另有带行号展示（read_file、edit_file 的回显与报错）与改动处的行范围。"""

import difflib
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from muliao.errors import InvalidInput
from muliao.platform.text_edit import Splice, locate_once, miss, occurrences, splice

# 带行号展示时单行的上限：一行压缩过的 JS/JSON 能吃掉整个输出预算
MAX_LINE_CHARS = 2000


class Edit(Protocol):
    """一处修改：按自己的方式在 current（同一份文本里前面的修改都做完后的样子）里定位，返回要做的各处替换（按位置
    排列、互不重叠）与替换数；定位不到就抛 InvalidInput。origin 是本次调用前的原文及其各行现在的位置，按行号定位的
    修改用它换算。只报替换、不自己拼新文本，Origin 才能确知哪些行被改过，不必事后比对猜。"""

    def locate(self, current: str, what: str, origin: "Origin") -> tuple[list[Splice], int]: ...


@dataclass(frozen=True)
class Replace:
    """按原文定位：old 恰好出现一次（规则同记忆与 skill，platform.text_edit.locate_once）替换为 new；every 时替换每一处
    （至少一处）。按原文定位用不到 origin。"""

    old: str
    new: str
    every: bool = False

    def locate(self, current: str, what: str, origin: "Origin") -> tuple[list[Splice], int]:
        if not self.every:
            at = [locate_once(current, self.old, what)]
        elif not self.old:
            raise InvalidInput("old text must not be empty")
        elif not (at := occurrences(current, self.old)):
            raise InvalidInput(miss(current, self.old, 0, f"old text must occur in {what}"))
        return [Splice(i, i + len(self.old), self.new) for i in at], len(at)


@dataclass(frozen=True)
class LineRange:
    """按行号定位：本次调用前（即 read_file 看到的）第 start 到 end 行（1 起算、含两端）整行替换为 new；同一次调用里
    前面的修改挪动了这些行时按 origin 换算到现在的位置，前面的修改碰过这些行就拒绝。anchor 是第 start 行的原文
    （两端空白不计），对不上说明行号已过期（读过之后文件又变了），报错并展示那几行的内容，不改错地方。"""

    start: int
    end: int
    anchor: str
    new: str

    def locate(self, current: str, what: str, origin: "Origin") -> tuple[list[Splice], int]:
        original = origin.text.splitlines(keepends=True)
        if not 1 <= self.start <= self.end:
            raise InvalidInput(
                f"need 1 <= start_line <= end_line, got start_line={self.start} end_line={self.end}"
            )
        if self.end > len(original):
            raise InvalidInput(f"end_line {self.end} is past the end: {what} has {len(original)} lines")
        if "\n" in self.anchor.strip():
            raise InvalidInput("anchor must be the text of the single line start_line, not several lines")
        line, anchor = original[self.start - 1].rstrip("\r\n"), self.anchor.strip()
        # 超长的行在 read_file 里是截断着展示的，照抄截断后的样子也算对上
        if anchor not in {line.strip(), _clip(line).strip()}:
            plain = origin.text.splitlines()
            first, last = max(self.start - 3, 0), min(self.end + 2, len(plain))
            raise InvalidInput(
                f"anchor does not match line {self.start} of {what}; lines {first + 1}-{last} are now:\n"
                f"{numbered(plain, first, last)}\ngive the line numbers and anchor of the text as it is now"
            )
        start, end = origin.now(self.start, self.end)
        starts = _line_starts(current)
        begin, stop = starts[start - 1], starts[end]
        return [Splice(begin, stop, as_lines(self.new, current[begin:stop]))], 1


def _line_starts(text: str) -> list[int]:
    """各行开头的字符位置，末尾再加上全文长度（第 i 行是 [starts[i], starts[i+1])）；分行规则同 str.splitlines。"""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _ends_line(text: str) -> bool:
    """text 以换行结尾（按 str.splitlines 的分行规则）。"""
    lines = text.splitlines(keepends=True)
    return bool(lines) and lines[-1] != lines[-1].splitlines()[0]


def _unchanged(old: list[str], new: list[str]) -> dict[int, int]:
    """一块整行改动里认作没变的行：old 的第 i 行 -> new 的第 j 行。只认首尾相同的行，而且先裁头再裁尾、先裁尾再裁头
    两种对法都认同一位置的才算：写出的新行里有和原行相同的（如 `}` 变成两个 `}`）时说不清哪一个是原来的，就算改掉了。"""

    def aligned(head_first: bool) -> set[tuple[int, int]]:
        same = min(len(old), len(new))
        head = tail = 0
        for side in ("head", "tail") if head_first else ("tail", "head"):
            if side == "head":
                while head < same - tail and old[head] == new[head]:
                    head += 1
            else:
                while tail < same - head and old[-1 - tail] == new[-1 - tail]:
                    tail += 1
        pairs = {(i, i) for i in range(head)}
        return pairs | {(len(old) - 1 - k, len(new) - 1 - k) for k in range(tail)}

    return dict(aligned(True) & aligned(False))


@dataclass
class _Block:
    """Origin.follow 里一块被改动的整行：原来的第 first 到 stop（不含）行；new 是块首到字符位置 at 这段改后的文本，
    at 之后到块尾是没动的原文。"""

    first: int
    stop: int
    new: str
    at: int

    def text(self, before: str, starts: list[int]) -> str:
        """这块改后的完整文本。"""
        return self.new + before[self.at : starts[self.stop]]


class Origin:
    """一份文本在本次调用前的原文，及原文各行在当前文本（做完前面几处修改后）里的位置：批量修改里行号一律指原文
    （模型手里的行号来自 read_file，看不到中间状态），由这里换算。每做完一处修改调用 follow 记下它改了哪些行。"""

    def __init__(self, text: str) -> None:
        self.text = text
        # 当前文本的每一行：(它在原文里是第几行（0 起），不是原文的行为 None；写下它的是第几处修改，原文为 0)
        self._lines: list[tuple[int | None, int]] = [(i, 0) for i in range(len(text.splitlines()))]
        # 被改掉或删掉的原文行 -> 改掉它的是第几处修改
        self._gone: dict[int, int] = {}

    def follow(self, before: str, places: Sequence[Splice], by: int) -> None:
        """第 by 处修改在当前文本 before 上做了 places 这些替换：每处替换扩到所在的整行（替换后不以换行结尾时连上下一行，
        落在同一行的几处合成一块），块外的行原样保留来历；块内只把确定没变的首尾行（_unchanged）认作原来那几行，其余原文行
        记为被第 by 处改掉，新写出的行记为第 by 处写的。只看替换本身、不比对整份文本，重复的行（空行、`}`）不会对错位置。"""
        starts = _line_starts(before)
        count = len(starts) - 1
        blocks: list[_Block] = []
        for start, end, new in places:
            first = bisect_right(starts, start) - 1
            if blocks and first < blocks[-1].stop:
                block = blocks[-1]
                block.new += before[block.at : start] + new
            else:
                block = _Block(first, first, before[starts[first] : start] + new, end)
                blocks.append(block)
            block.at = end
            if end > start:
                touched = bisect_right(starts, end - 1)
            else:
                # 纯插入：插在行首且自成整行的不碰这一行
                touched = first if start == starts[first] and (not new or _ends_line(new)) else first + 1
            block.stop = max(block.stop, min(touched, count))
            # 块的新文本不以换行结尾时，和后面一行连成了一行
            while block.stop < count and not _ends_line(text := block.text(before, starts)) and text:
                block.stop += 1
        lines: list[tuple[int | None, int]] = []
        kept = 0
        for block in blocks:
            lines += self._lines[kept : block.first]
            old = before[starts[block.first] : starts[block.stop]].splitlines(keepends=True)
            written = block.text(before, starts).splitlines(keepends=True)
            stays = _unchanged(old, written)
            for i, (original, _) in enumerate(self._lines[block.first : block.stop]):
                if original is not None and i not in stays:
                    self._gone[original] = by
            moved = {j: block.first + i for i, j in stays.items()}
            lines += [self._lines[moved[j]] if j in moved else (None, by) for j in range(len(written))]
            kept = block.stop
        lines += self._lines[kept:]
        after = len(splice(before, places).splitlines())
        if len(lines) != after:
            # 拼接处生出新的换行（如 "\r" 接上 "\n"）时对不上：不再换算，之后的行号区间一律当作重叠拒绝
            self._gone.update(dict.fromkeys(range(len(self.text.splitlines())), by))
            lines = [(None, by)] * after
        self._lines = lines

    def now(self, start: int, end: int) -> tuple[int, int]:
        """原文第 start 到 end 行（1 起算、含两端）现在是第几到第几行；其中有行被前面的修改改掉、删掉，或中间被插入了
        新行，就是两处修改重叠，拒绝。"""
        for k in range(start - 1, end):
            if k in self._gone:
                raise self._overlap(start, end, self._gone[k])
        at = {original: i for i, (original, _) in enumerate(self._lines) if original is not None}
        first, last = at[start - 1], at[end - 1]
        for i in range(first, last + 1):
            if self._lines[i][0] is None:
                raise self._overlap(start, end, self._lines[i][1])
        return first + 1, last + 1

    @staticmethod
    def _overlap(start: int, end: int, by: int) -> InvalidInput:
        lines = f"line {start}" if start == end else f"lines {start}-{end}"
        return InvalidInput(
            f"{lines} overlap edit {by}, which already changed that part; merge the two into one edit"
        )


def as_lines(new: str, replaced: str) -> str:
    """替换整行时的新内容：被替换的行以换行结尾而 new 没有时补上，new 末尾的换行因此可写可不写；new 为空即删掉这些行。"""
    if new and replaced.endswith("\n") and not new.endswith("\n"):
        return new + "\n"
    return new


def apply_each[K](
    texts: Mapping[K, str], edits: Sequence[tuple[K, Edit]]
) -> tuple[dict[K, str], dict[K, int]]:
    """依次做多处修改，每项作用在它那份文本上（键是文本的名字，如文件路径）。同一份文本里按内容定位的（原文、语法
    模式）在前面改过的结果上找；按行号定位的，行号一律指调用前的原文，按 Origin 换算。返回各份的新文本与替换数（只含
    被改到的键）。任何一项失败都整体失败，报错指出是第几项；调用方只在成功后写盘，所以不会因为某处对不上而留下改了
    一半的文件（写盘本身的 IO 失败不归这里管）。"""
    if not edits:
        raise InvalidInput("no edits given")
    current: dict[K, str] = {}
    origins: dict[K, Origin] = {}
    counts: dict[K, int] = {}
    for i, (key, change) in enumerate(edits, 1):
        try:
            if key not in origins:
                origins[key] = Origin(texts[key])
            origin = origins[key]
            text = current.get(key, origin.text)
            places, count = change.locate(text, str(key), origin)
        except InvalidInput as e:
            if len(edits) == 1:
                raise
            k = sum(1 for earlier, _ in edits[: i - 1] if earlier == key)
            after = (
                f" (old_text/pattern are matched against the text with the {k} earlier edit(s) to it applied; "
                "line numbers refer to the text before this call)"
                if k
                else ""
            )
            raise InvalidInput(f"edit {i} of {len(edits)}{after}: {e}; nothing was written") from e
        current[key] = splice(text, places)
        origin.follow(text, places, i)
        counts[key] = counts.get(key, 0) + count
    return current, counts


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


def numbered(lines: list[str], start: int, end: int) -> str:
    """lines[start:end]，按 `cat -n` 的格式：1 起算的行号右对齐占 6 列，后跟一个制表符（read_file、edit_file 的回显与
    报错同一格式，模型拿来就认得），过长的行截断。"""
    return "\n".join(f"{i + 1:6}\t{_clip(lines[i])}" for i in range(start, end))


def _clip(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    return f"{line[:MAX_LINE_CHARS]}…[line truncated: {len(line) - MAX_LINE_CHARS} more chars]"
