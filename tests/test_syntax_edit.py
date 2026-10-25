"""edit_file 的行号区间（text_edit.LineRange）与语法结构（syntax_edit.PatternEdit）定位：纯函数，直接测规则。"""

import time
from pathlib import PurePath

import pytest

from muliao.errors import InvalidInput
from muliao.platform.syntax_edit import LANGUAGES, PatternEdit, language_of
from muliao.platform.text_edit import LineRange, Replace, apply_each

PY = '''import pytest


def test_a():
    assert 1


@pytest.mark.parametrize("x", [1, 2])
def test_b(x):
    """中文说明"""
    assert x


def test_c():
    assert 3
'''


def py(pattern: str, new: str, every: bool = False) -> PatternEdit:
    return PatternEdit(pattern, "python", new, every)


# ---- 行号区间 ----


def test_line_range_replaces_whole_lines_and_adds_the_missing_trailing_newline():
    text = "a\nb\nc\nd\n"
    assert LineRange(2, 3, "b", "X").apply(text, "f") == ("a\nX\nd\n", 1)
    assert LineRange(2, 3, "  b  ", "X\nY\n").apply(text, "f") == ("a\nX\nY\nd\n", 1)
    assert LineRange(1, 4, "a", "").apply(text, "f") == ("", 1)
    # 最后一行没有换行时不补
    assert LineRange(2, 2, "b", "X").apply("a\nb", "f") == ("a\nX", 1)


def test_line_range_anchor_may_be_copied_from_a_truncated_long_line():
    long = "x" * 2500
    shown = long[:2000] + "…[line truncated]"
    assert LineRange(1, 1, shown, "y").apply(f"{long}\nb\n", "f") == ("y\nb\n", 1)


def test_line_range_with_a_stale_anchor_fails_and_shows_the_lines_as_they_are_now():
    text = "".join(f"l{i}\n" for i in range(1, 11))
    with pytest.raises(InvalidInput) as e:
        LineRange(5, 6, "l4", "X").apply(text, "f.txt")
    message = str(e.value)
    assert "anchor does not match line 5 of f.txt" in message
    assert "lines 3-8 are now:\n3\tl3\n4\tl4\n5\tl5\n6\tl6\n7\tl7\n8\tl8" in message


@pytest.mark.parametrize(
    ("start", "end", "anchor", "error"),
    [
        (0, 1, "a", "need 1 <= start_line <= end_line"),
        (3, 2, "c", "need 1 <= start_line <= end_line"),
        (2, 9, "b", "end_line 9 is past the end: f has 3 lines"),
        (1, 1, "a\nb", "single line"),
    ],
)
def test_line_range_rejects_bad_ranges(start, end, anchor, error):
    with pytest.raises(InvalidInput, match=error):
        LineRange(start, end, anchor, "X").apply("a\nb\nc\n", "f")


def test_line_numbers_of_later_edits_count_on_the_text_after_earlier_ones():
    text = {"f": "a\nb\nc\nd\n"}
    # 自下而上列出，互不影响
    assert apply_each(text, [("f", LineRange(4, 4, "d", "D")), ("f", LineRange(1, 1, "a", "A\nA2"))]) == (
        {"f": "A\nA2\nb\nc\nD\n"},
        {"f": 2},
    )
    # 先在上面插入一行，下面的行号就过期了：锚点挡住，整体失败
    with pytest.raises(InvalidInput, match="edit 2 of 2.*1 earlier edit.*anchor does not match line 4"):
        apply_each(text, [("f", Replace("a\n", "a\nnew\n")), ("f", LineRange(4, 4, "d", "D"))])


def test_edits_to_several_texts_each_see_only_their_own_earlier_edits():
    texts = {"f": "a\nb\n", "g": "a\nb\n", "untouched": "x\n"}
    edits = [
        ("f", Replace("a\n", "a\nnew\n")),
        ("g", LineRange(2, 2, "b", "B")),
        ("f", LineRange(3, 3, "b", "B")),
    ]
    # 第 1 项在 f 里插了一行，g 的行号不受影响；没改到的文本不在结果里
    assert apply_each(texts, edits) == ({"f": "a\nnew\nB\n", "g": "a\nB\n"}, {"f": 2, "g": 1})
    # 失败时报的「之前改过几处」只算同一份文本的
    with pytest.raises(InvalidInput, match=r"edit 3 of 3: old text must occur exactly once in g") as caught:
        apply_each(texts, [("f", Replace("a", "z")), ("f", Replace("b", "y")), ("g", Replace("zz", "w"))])
    assert "earlier" not in str(caught.value)


# ---- 语法结构 ----


def test_language_comes_from_the_extension_and_unknown_ones_are_refused():
    assert language_of(PurePath("x/a.py")) == "python"
    assert language_of(PurePath("A.TSX")) == "tsx"
    for name in ("a.ts", "a.js", "a.jsx", "a.mjs"):
        assert language_of(PurePath(name)) in {"typescript", "javascript"}
    with pytest.raises(InvalidInput, match="extension '.toml'"):
        language_of(PurePath("pyproject.toml"))
    with pytest.raises(InvalidInput, match="extension '\\(none\\)'"):
        language_of(PurePath("Makefile"))


def test_every_mapped_language_is_built_into_ast_grep():
    from ast_grep_py import SgRoot

    for language in set(LANGUAGES.values()):
        SgRoot("x", language).root()


def test_deleting_a_function_removes_its_decorators_and_the_extra_blank_lines():
    out, count = py("def test_b", "").apply(PY, "t.py")
    assert count == 1
    assert out == "import pytest\n\n\ndef test_a():\n    assert 1\n\n\ndef test_c():\n    assert 3\n"
    # 文件末尾的函数：不留尾随空行
    out, _ = py("def test_c", "").apply(PY, "t.py")
    assert out.endswith("    assert x\n") and "test_c" not in out
    # 文件开头
    out, _ = py("import pytest", "").apply(PY, "t.py")
    assert out.startswith("def test_a():")


def test_replacement_keeps_the_indentation_and_indents_later_lines_of_new_text_like_the_match():
    text = "class K:\n    def m(self):\n        return 1\n\n    def n(self):\n        return 2\n"
    out, _ = py("def m", "def m(self):\n    return 10").apply(text, "k.py")
    assert out == "class K:\n    def m(self):\n        return 10\n\n    def n(self):\n        return 2\n"
    # 元变量捕获的多行原文保持原来的缩进
    text = "class K:\n    def m(self):\n        a = 1\n        return a\n"
    out, _ = py("def m($$$P): $$$B", "def m2($$$P):\n    $$$B").apply(text, "k.py")
    assert out == "class K:\n    def m2(self):\n        a = 1\n        return a\n"


def test_a_multi_line_capture_moved_one_level_deeper_is_reindented_as_a_block():
    text = "class C:\n    def m(self):\n        a = 1\n\n        b = 2\n"
    out, _ = py("def m(self): $$$B", "def m(self):\n    with lock:\n        $$$B").apply(text, "c.py")
    assert out == "class C:\n    def m(self):\n        with lock:\n            a = 1\n\n            b = 2\n"


def test_a_multi_line_capture_on_the_first_line_of_new_text_or_one_level_up_keeps_its_relative_indentation():
    text = "def f():\n    print(dict(\n        a=1,\n    ))\n"
    assert (
        py("print($A)", "log($A)").apply(text, "a.py")[0] == "def f():\n    log(dict(\n        a=1,\n    ))\n"
    )
    text = "def f():\n    x = foo(\n        1,\n        2,\n    )\n"
    out, _ = py("foo($$$A)", "bar(0, $$$A)").apply(text, "a.py")
    # 同 ast-grep：后续行相对元变量所在行（x = ... 那行）重排，仍在括号里，合法
    assert out == "def f():\n    x = bar(0, 1,\n    2,)\n"
    text = "def f():\n    with lock:\n        if a:\n            b()\n"
    assert py("with lock: $$$B", "$$$B").apply(text, "a.py")[0] == "def f():\n    if a:\n        b()\n"
    # 比起始行还浅的行（三引号字符串的内容）同层代入时一字不改
    text = "class K:\n    def m(self):\n        s = '''\nraw\n'''\n        return s\n"
    out, _ = py("def m($$$P): $$$B", "def n($$$P):\n    $$$B").apply(text, "k.py")
    assert out == text.replace("def m", "def n")


def test_a_trailing_newline_in_new_text_does_not_add_a_blank_line_at_the_end_of_a_line():
    text = "def f():\n    a = 1\n    b = 2\n"
    assert py("a = 1", "a = 3\n").apply(text, "a.py")[0] == "def f():\n    a = 3\n    b = 2\n"


def test_replace_all_across_indentation_levels_keeps_each_ones_indentation():
    text = "def f():\n    print(1)\n    if x:\n        print(2)\nprint(3)\n"
    out, count = py("print($A)", "log($A)", every=True).apply(text, "a.py")
    assert (out, count) == ("def f():\n    log(1)\n    if x:\n        log(2)\nlog(3)\n", 3)
    out, _ = py("if $C: $$$B", "while $C:\n    $$$B").apply(text, "a.py")
    assert out == "def f():\n    print(1)\n    while x:\n        print(2)\nprint(3)\n"
    js = "function f() {\n  console.log(1)\n}\n"
    out, _ = PatternEdit("console.log($A)", "javascript", "logger.info($A)").apply(js, "a.js")
    assert out == "function f() {\n  logger.info(1)\n}\n"


def test_a_match_inside_a_line_replaces_only_the_matched_text_and_metavariables_carry_over():
    text = "x = foo(1, 2) + 3\ny = foo(a)\n"
    out, count = py("foo($$$ARGS)", "bar($$$ARGS, $HOME)", every=True).apply(text, "a.py")
    # 模式里没声明的 $HOME 原样保留
    assert (out, count) == ("x = bar(1, 2, $HOME) + 3\ny = bar(a, $HOME)\n", 2)
    # $$A 不是元变量引用
    out, _ = py("foo($A)", "g($$A, $A)").apply("foo(1)\n", "a.py")
    assert out == "g($$A, 1)\n"
    out, _ = py("def test_a($$$P): $$$BODY", "def test_a2($$$P):\n    $$$BODY\n    assert 2").apply(
        PY, "t.py"
    )
    assert "def test_a2():\n    assert 1\n    assert 2\n\n\n@pytest" in out


def test_pattern_must_match_exactly_one_place_unless_replace_all():
    with pytest.raises(InvalidInput, match=r"matches 3 places in t.py \(at lines 4, 9, 14\).*replace_all"):
        py("def $F($$$A): $$$B", "").apply(PY, "t.py")
    with pytest.raises(InvalidInput, match="matches nothing in t.py"):
        py("def test_zzz", "").apply(PY, "t.py")
    with pytest.raises(InvalidInput, match="invalid python pattern"):
        py("x = 1\ny = 2", "").apply(PY, "t.py")
    with pytest.raises(InvalidInput, match="overlap"):
        py("foo($A)", "bar", every=True).apply("foo(foo(1))\n", "a.py")


def test_replace_all_deletes_every_match():
    out, count = py("def $F($$$A): $$$B", "", every=True).apply(PY, "t.py")
    assert (out, count) == ("import pytest\n", 3)


def test_typescript_export_goes_with_the_deleted_node():
    ts = (
        "import x from 'x'\n\nexport function f(a: number) {\n  return a\n}\n\nclass A {\n  m(): void {}\n}\n"
    )
    out, _ = PatternEdit("function f($$$A) { $$$B }", "typescript", "").apply(ts, "a.ts")
    assert out == "import x from 'x'\n\nclass A {\n  m(): void {}\n}\n"
    out, _ = PatternEdit("class A { $$$B }", "typescript", "class B {}").apply(ts, "a.ts")
    assert out.endswith("}\n\nclass B {}\n")


def test_tsx_and_javascript_parse():
    tsx = "export const App = () => <div>{x}</div>\n"
    out, _ = PatternEdit("<div>{$X}</div>", "tsx", "<span>{$X}</span>").apply(tsx, "a.tsx")
    assert out == "export const App = () => <span>{x}</span>\n"
    out, _ = PatternEdit("console.log($A)", "javascript", "").apply("a()\nconsole.log(1)\nb()\n", "a.js")
    assert out == "a()\nb()\n"


def test_a_few_thousand_lines_parse_and_match_quickly():
    text = "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(2000))
    started = time.perf_counter()
    out, _ = py("def f1999", "").apply(text, "big.py")
    assert time.perf_counter() - started < 1
    assert "f1999" not in out and out.endswith("return x + 1998\n")
