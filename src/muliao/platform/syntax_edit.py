"""按语法结构定位要改的块（edit_file 的 `pattern`）：用 ast-grep（tree-sitter）按模式找到语法节点再替换，免得把
几十行原文逐字贴进 old_text。语言按文件扩展名定，修改的规则（整行、删除时的空行、多处）见 PatternEdit。"""

import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import PurePath

from ast_grep_py import SgNode, SgRoot

from muliao.errors import InvalidInput
from muliao.platform.text_edit import listed

# 扩展名 -> ast-grep 内置语言名（只列 ast-grep-py 自带的语言；不认识的扩展名直接拒绝，不猜）
LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".lua": "lua",
    ".php": "php",
    ".scala": "scala",
    ".hs": "haskell",
    ".ex": "elixir",
    ".exs": "elixir",
    ".nix": "nix",
    ".sol": "solidity",
    ".sh": "bash",
    ".bash": "bash",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".tf": "hcl",
    ".hcl": "hcl",
}

# 删除一个节点时连同删掉的外层：Python 的装饰器包装、JS/TS 的 export，否则会留下悬空的装饰器（它会挂到下一个定义上）
# 或孤零零的 export
_WRAPPERS = frozenset({"decorated_definition", "export_statement"})
# 模式里的元变量（$A、$$$ARGS；以 _ 开头的不捕获，不算；$$A 是 ast-grep 匹配匿名节点的写法，也不算）
_METAVAR = re.compile(r"(?<!\$)\$(?:\$\$)?([A-Z][A-Z0-9_]*)")


def language_of(path: PurePath) -> str:
    language = LANGUAGES.get(path.suffix.lower())
    if language is None:
        known = " ".join(sorted(LANGUAGES))
        raise InvalidInput(
            f"pattern needs a source file of a known language, but {path.name} has extension "
            f"{path.suffix or '(none)'!r} (known: {known}); locate the text with old_text or start_line/end_line"
        )
    return language


@dataclass(frozen=True)
class PatternEdit:
    """按 ast-grep 模式定位：模式必须恰好匹配一处（every 时每一处，彼此不能嵌套），替换为 new，new 里可以用模式的元变量。
    只替换匹配到的代码，行首原有缩进保留；new 的第 2 行起补上匹配处所在行的缩进（同 ast-grep rewrite），所以 new 按相对缩进来写，
    replace_all 改不同缩进层级的多处也不会错。new 为空即删除：连同装饰器包装、export 一起删，占满整行时删掉这些行，
    删除处两侧的空行合并成两侧原有空行数中较多的那一份（在文件开头或末尾则不留）。"""

    pattern: str
    language: str
    new: str
    every: bool = False

    def apply(self, current: str, what: str) -> tuple[str, int]:
        root = SgRoot(current, self.language).root()
        try:
            matches = root.find_all(pattern=self.pattern)
        except RuntimeError as e:
            # ast-grep 的错误是多行的「原因链」，最后一行才是具体原因（如模式解析成了多个节点）
            reason = re.sub(r"^\d+:\s*", "", str(e).strip().splitlines()[-1].strip())
            raise InvalidInput(f"invalid {self.language} pattern {self.pattern!r}: {reason}") from e
        if not matches:
            raise InvalidInput(
                f"pattern {self.pattern!r} matches nothing in {what} (parsed as {self.language}); it is code with "
                "$VAR / $$$VARS wildcards that must match whole syntax nodes, e.g. `def name` for a Python "
                "function, or re-read the file and use old_text"
            )
        at = sorted({m.range().start.line + 1 for m in matches})
        if len(matches) > 1 and not self.every:
            raise InvalidInput(
                f"pattern matches {len(matches)} places in {what} (at lines {listed(at)}); make it more specific, "
                "or set replace_all to change every one"
            )
        spans = sorted(self._span(current, m) for m in matches)
        for (_, end, _), (start, _, _) in pairwise(spans):
            if start < end:
                raise InvalidInput(
                    f"pattern matches overlap in {what} (one is inside another; matches at lines {listed(at)}): "
                    "make it more specific"
                )
        # 从后往前改，前面各处的位置不变
        for start, end, text in reversed(spans):
            current = _splice(current, start, end, text)
        return current, len(spans)

    def _span(self, text: str, match: SgNode) -> tuple[int, int, str]:
        """这一处要替换的 [起, 止) 字符位置与替换成的文本（ast-grep 的 index 按字符计，与 str 下标一致）。"""
        if not self.new:
            node = _with_wrappers(match)
            start, end = node.range().start.index, node.range().end.index
            line_start = text.rfind("\n", 0, start) + 1
            newline = text.find("\n", end)
            line_end = len(text) if newline < 0 else newline + 1
            if text[line_start:start].strip() or text[end:line_end].strip():
                return start, end, ""
            return line_start, line_end, ""
        start, end = match.range().start.index, match.range().end.index
        indent = _indent_before(text, start)
        new = self.new
        # 匹配处在行尾时，这一行自己的换行还在；new 末尾顺手带的换行会多出一个空行
        newline = text.find("\n", end)
        if new.endswith("\n") and not text[end : len(text) if newline < 0 else newline].strip():
            new = new[:-1]
        # 先给模板补上匹配处的缩进，再代入元变量（代入时按元变量在模板里所在行的缩进重排捕获的多行原文）
        template = re.sub(r"\n(?=[^\n])", "\n" + indent, new)
        declared = frozenset(_METAVAR.findall(self.pattern))
        # 模板第一行接在匹配处所在行的前半截后面：元变量所在行的缩进要连它一起算
        prefix = text[text.rfind("\n", 0, start) + 1 : start]
        return start, end, _METAVAR.sub(lambda m: _captured(text, match, declared, prefix, m), template)


def _captured(text: str, match: SgNode, declared: frozenset[str], prefix: str, metavar: re.Match[str]) -> str:
    """new 里一个元变量换成的原文；多节点元变量（$$$X）取首个节点开头到末个节点结尾的原文，保留其间的分隔与换行，
    一个节点都没匹配到（如没有参数的函数）时为空。模式里没声明的原样保留（new 里可能本来就有 `$HOME` 这样的文字）。
    多行的原文同 ast-grep rewrite 重排缩进：第 2 行起去掉它在文件里起始行的缩进、换成元变量在模板里所在行的缩进，
    所以把 $$$BODY 放进更深一层（如包一层 with）时整块跟着进去。比起始行还浅的行（如三引号字符串的内容）原样不动。
    prefix 是匹配处所在行在匹配之前的部分（模板第一行接在它后面）。"""
    name = metavar.group(1)
    if name not in declared:
        return metavar.group(0)
    single = match.get_match(name)
    nodes = [single] if single is not None else match.get_multiple_matches(name)
    if not nodes:
        return ""
    start = nodes[0].range().start.index
    captured = text[start : nodes[-1].range().end.index]
    if "\n" not in captured:
        return captured
    old_indent = _indent_before(text, start)
    new_indent = _indent_before(prefix + metavar.string, len(prefix) + metavar.start())
    first, *rest = captured.split("\n")
    moved = [
        new_indent + line.removeprefix(old_indent) if line.strip() and line.startswith(old_indent) else line
        for line in rest
    ]
    return "\n".join([first, *moved])


def _indent_before(text: str, at: int) -> str:
    """at 所在行的前导空白。"""
    line = text[text.rfind("\n", 0, at) + 1 : at]
    return line[: len(line) - len(line.lstrip())]


def _with_wrappers(node: SgNode) -> SgNode:
    while (parent := node.parent()) is not None and parent.kind() in _WRAPPERS:
        if parent.range().end.index != node.range().end.index:
            break
        node = parent
    return node


def _splice(text: str, start: int, end: int, new: str) -> str:
    """把 text[start:end] 换成 new；整行删除（new 为空且 start、end 都在行首）时合并删除处两侧的空行。"""
    before, after = text[:start], text[end:]
    whole_lines = (
        not new and (start == 0 or text[start - 1] == "\n") and (end == len(text) or text[end - 1] == "\n")
    )
    if not whole_lines:
        return before + new + after
    above, below = before.splitlines(keepends=True), after.splitlines(keepends=True)
    blank_above = _count_blank(reversed(above))
    blank_below = _count_blank(below)
    kept_above, kept_below = above[: len(above) - blank_above], below[blank_below:]
    blanks = above[len(kept_above) :] + below[:blank_below]
    keep = max(blank_above, blank_below) if kept_above and kept_below else 0
    return "".join(kept_above + blanks[:keep] + kept_below)


def _count_blank(lines) -> int:
    count = 0
    for line in lines:
        if line.strip():
            break
        count += 1
    return count
