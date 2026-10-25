"""文件与 shell 工具。相对路径以 agent 工作目录为基准；不做强隔离（单人本机使用，见架构文档）。

这里是文件工具与前台执行；组合进运行的是 jobs.tools.workspace_toolset，它在此之上加上后台任务。
"""

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import BinaryContent, FunctionToolset, RunContext, ToolReturn

from muliao.agent_runtime.conventions import TOOL_ATTACHMENTS, attachment_of
from muliao.errors import InvalidInput, NotFound
from muliao.files.api import INLINE_IMAGE_TYPES, FileStore
from muliao.platform import text_edit
from muliao.platform.shell import run_bash
from muliao.platform.syntax_edit import PatternEdit, language_of
from muliao.platform.text_edit import numbered, truncate
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.vault.api import Vault

log = logging.getLogger(__name__)

# edit_file 成功后展示的改动前后上下文行数：够确认改对了，不必再回读文件
_EDIT_CONTEXT_LINES = 2
_DEFAULT_READ_LIMIT = 2000
# read_file 按行截断时，正文之外每段还要放的字符数的上界：`==> path lines A-B of N <==` 标题里路径以外的部分、
# 续读说明、段间分隔（正文预算 = 剩余预算 - 路径长 - 它）
_PART_OVERHEAD = 200
# read_file 交给模型看的图片上限（文件字节数）：Anthropic API 单张图片限 5MB，按 base64 编码后的长度算，即原始字节
# 的 3/4（其他 provider 更宽）。超过就拒绝、不在这里缩放：放进历史后每次请求都会被 provider 拒绝
_MAX_IMAGE_BYTES = 5 * 1024 * 1024 * 3 // 4


class TextEdit(BaseModel):
    # edit_file 一次调用里的一处修改（类 docstring 会进 schema 给模型看，所以用注释）；定位方式与 edit_file 的同名参数一致
    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(default=None, description="File to change; defaults to the top-level path.")
    old_text: str | None = Field(default=None, description="Exact text to replace.")
    start_line: int | None = Field(default=None, description="First line to replace (1-based).")
    end_line: int | None = Field(default=None, description="Last line to replace (inclusive).")
    anchor: str | None = Field(default=None, description="The current text of line start_line.")
    pattern: str | None = Field(default=None, description="ast-grep pattern matching the code to replace.")
    new_text: str = Field(description="Replacement text.")
    replace_all: bool = Field(
        default=False,
        description="Replace every occurrence (old_text) or match (pattern) instead of exactly one.",
    )


class FileRange(BaseModel):
    # read_file 一次调用里要读的一段
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="File path.")
    offset: int = Field(default=0, description="First line to return (0-based).")
    limit: int = Field(default=_DEFAULT_READ_LIMIT, description="Maximum number of lines to return.")


@dataclass(frozen=True)
class ShellLimits:
    default_timeout: float
    max_output_chars: int


def resolve(scope: RunScope, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else scope.workspace / p


_SHELL_DESCRIPTION = (
    "Run a shell command (bash) in your workspace and return its exit code and combined output. "
    "Commands are killed after {default:g} seconds unless you pass a different `timeout`. "
    "Only the first {max_output} characters of output are kept, so limit it yourself: pipe through `| head` or `| tail`, "
    "use `rg -m N`, `cut -c1-200`, and read only the part of a long log you need. "
    "Search with ripgrep, not grep/find: `rg -n PATTERN [PATH]` for contents (`-g '*.py'` filters files, `-C 3` "
    "adds context), `rg --files [-g GLOB]` to find files; it skips .git and ignored files such as node_modules. "
    "Read files with read_file (one call can read several files or parts, with line numbers) and change them "
    "with edit_file / write_file rather than cat, sed or heredocs."
)


def shell_description(limits: ShellLimits) -> str:
    """时限与输出上限是配置的值，写进说明：模型不知道它们，长命令被杀、长输出被截了才学会。"""
    return _SHELL_DESCRIPTION.format(default=limits.default_timeout, max_output=limits.max_output_chars)


async def run_foreground(
    vault: Vault,
    limits: ShellLimits,
    scope: RunScope,
    command: str,
    timeout: float | None,  # noqa: ASYNC109  run_shell 的参数原样传入
    secrets: tuple[str, ...],
) -> str:
    timeout = limits.default_timeout if timeout is None else timeout
    if not timeout > 0:  # 也拒绝 NaN
        raise InvalidInput("timeout must be greater than 0 seconds")
    secret_values = await vault.resolve_many(scope.agent_id, list(secrets))
    result = await run_bash(
        command, cwd=scope.workspace, secrets=secret_values, timeout=timeout, merge_stderr=True
    )
    status = (
        f"exit_code: {result.exit_code}"
        if result.exit_code is not None
        else f"killed after {timeout}s timeout"
    )
    return f"{status}\n{truncate(result.stdout, limits.max_output_chars)}"


def file_toolset(limits: ShellLimits, files: FileStore) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def read_file(
        ctx: RunContext[RunScope],
        path: str | None = None,
        offset: int = 0,
        limit: int = _DEFAULT_READ_LIMIT,
        reads: tuple[FileRange, ...] = (),
    ) -> str | list[str | BinaryContent]:
        """Read a text file; lines are numbered. Relative paths are resolved against your workspace.
        To read several files or several parts of one file in one call, pass `reads` instead of
        `path`/`offset`/`limit`; each part is shown under a `==> path lines A-B of N <==` header.
        Output is limited in size: when the limit is reached, reading stops after a whole line and says which
        offset to continue from (parts not read at all are listed).
        An image file (PNG, JPEG, GIF, WebP) passed as `path` is returned as an image you can see, e.g. a
        screenshot or a chart you made.

        Args:
            path: File path (omit when using `reads`).
            offset: First line to return (0-based).
            limit: Maximum number of lines to return.
            reads: List of {path, offset, limit} parts to read, shown in this order; a part that cannot be
                read shows its error and the others are still returned.
        """
        if reads and (path is not None or offset != 0 or limit != _DEFAULT_READ_LIMIT):
            raise InvalidInput("pass either path/offset/limit or reads, not both")
        if not reads:
            if path is None:
                raise InvalidInput("path is required (or pass reads)")
            target = resolve(ctx.deps, path)
            if media_type := _image_type(target):
                if offset != 0 or limit != _DEFAULT_READ_LIMIT:
                    raise InvalidInput(f"offset/limit do not apply to an image file: {target}")
                return _read_image(target, media_type)
            excerpt = _read_range(target, offset, limit, limits.max_output_chars - _PART_OVERHEAD)
            # 按行截断后正文已在预算内；只有第一行就比整个预算还长时这里才会再截
            return truncate(excerpt.body, limits.max_output_chars) + excerpt.more_note
        return _read_parts(ctx.deps, reads, limits.max_output_chars)

    @ts.tool
    async def write_file(ctx: RunContext[RunScope], path: str, content: str) -> str:
        """Create or overwrite a text file. Parent directories are created as needed.

        Args:
            path: File path.
            content: Full file content.
        """
        target = resolve(ctx.deps, path)
        existed = target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        verb = "overwrote" if existed else "created"
        return f"{verb} {target} ({len(content.splitlines())} lines, {len(content)} chars)"

    @ts.tool
    async def edit_file(
        ctx: RunContext[RunScope],
        path: str | None = None,
        old_text: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        anchor: str | None = None,
        pattern: str | None = None,
        new_text: str | None = None,
        replace_all: bool = False,
        edits: tuple[TextEdit, ...] = (),
    ) -> str:
        """Change a file: locate a block in exactly one of three ways and replace it with `new_text`.
        - `old_text`: exact text that must occur exactly once (unless `replace_all`). Copy it from the file as it
          is now (without read_file's line-number prefix), with enough surrounding lines to be unique.
        - `start_line` + `end_line` + `anchor`: whole lines start_line..end_line as numbered by read_file;
          `anchor` is the current text of line start_line, checked so that stale line numbers fail instead of
          changing the wrong lines. Good for replacing or deleting a long block without copying it.
        - `pattern`: an ast-grep pattern for a source file (language from the extension): code where `$VAR`
          matches one syntax node and `$$$VARS` any number of them, e.g. `def test_x` matches that whole Python
          function (with its body), `function f($$$A) { $$$B }` a JavaScript one. It must match exactly one place
          (unless `replace_all`); `new_text` may use its metavariables (`$VAR` becomes the matched code).
          Only the matched code is replaced and the indentation in front of it stays; lines 2+ of `new_text`
          get the match's indentation added, so indent them relative to its first line; a multi-line
          `$$$VARS` is re-indented to where you put it (e.g. one level deeper inside a new `with`).
          Deleting a match also removes its decorators / `export`, and its lines with the extra blank lines
          left behind.
        To change several places in one call, in one file or in several files, pass `edits` instead: each edit
        changes its own `path` (or the top-level `path` when it has none); they are applied in order, each seeing
        the result of the earlier ones in the same file (so list line-range edits from the bottom of the file
        up), and if any of them fails no file is written. On success the changed lines of each file are shown
        with their numbers.

        Args:
            path: File path (with `edits`: the file of the edits that give no path of their own).
            old_text: Exact text to replace (omit when using `edits`).
            start_line: First line to replace (1-based); give with `end_line` and `anchor`.
            end_line: Last line to replace (inclusive).
            anchor: The current text of line start_line (leading/trailing whitespace ignored).
            pattern: ast-grep pattern matching the code to replace.
            new_text: Replacement text; empty deletes the located text (omit when using `edits`).
            replace_all: Replace every occurrence of `old_text` or every match of `pattern` (e.g. renaming a
                variable) instead of exactly one (omit when using `edits`).
            edits: List of changes, each {new_text} plus one way to locate it (old_text,
                start_line/end_line/anchor or pattern), optionally replace_all, and optionally its own path,
                applied in order (each sees the result of the earlier ones in the same file).
        """
        single = (old_text, start_line, end_line, anchor, pattern, new_text)
        if edits and (any(v is not None for v in single) or replace_all):
            raise InvalidInput("pass either one change (old_text/.../new_text) or edits, not both")
        if not edits:
            if path is None:
                raise InvalidInput("path is required")
            if new_text is None:
                raise InvalidInput("new_text is required (or pass edits)")
            edits = (
                TextEdit(
                    old_text=old_text,
                    start_line=start_line,
                    end_line=end_line,
                    anchor=anchor,
                    pattern=pattern,
                    new_text=new_text,
                    replace_all=replace_all,
                ),
            )
        # 同一个文件可能写成不同路径（相对/绝对、带 ..）：按真实路径认成一份，用第一次出现时的写法展示，
        # 否则后写的一份会覆盖先写的
        same: dict[Path, Path] = {}
        before: dict[Path, str] = {}
        located: list[_Located] = []
        for i, e in enumerate(edits, 1):
            given = _edit_target(ctx.deps, path, e, i)
            if not given.is_file():
                raise NotFound(f"file not found: {given}")
            target = same.setdefault(given.resolve(), given)
            if target not in before:
                before[target] = target.read_text(encoding="utf-8")
            located.append(_Located(target, e))
        after, counts = text_edit.apply_each(before, [(c.target, c) for c in located])
        # 全部校验通过才写盘
        for target, content in after.items():
            target.write_text(content, encoding="utf-8")
        reports = [_edit_report(t, before[t], after[t], counts[t]) for t in after]
        return truncate("\n\n".join(reports), limits.max_output_chars)

    @ts.tool
    async def share_file(ctx: RunContext[RunScope], path: str) -> ToolReturn:
        """Share a file from your workspace with the user, e.g. a chart you made or a document you wrote.
        It is shown to the user with your reply automatically (images inline, other files as downloads), so do
        not embed or link it yourself; just refer to it by name. The user gets a snapshot; later changes to the
        file are not reflected.

        Args:
            path: File path.
        """
        target = resolve(ctx.deps, path)
        if not target.is_file():
            raise NotFound(f"file not found: {target}")
        meta = await files.put_path(target.name, _media_type(target), target)
        log.info(
            "file shared with user",
            extra={"agent": ctx.deps.agent_id, "session_id": ctx.deps.session_id, "file_id": meta.id},
        )
        return ToolReturn(
            return_value=f"Shared {meta.name!r} with the user; it is shown with your reply.",
            metadata={TOOL_ATTACHMENTS: [attachment_of(meta)]},
        )

    return ts


@dataclass(frozen=True)
class _Excerpt:
    # 「lines A-B of N」，多段读取时作为每段的标题
    span: str
    # 带行号的正文
    body: str
    # 后面还有行时注明还剩多少（因输出上限停下时写明从哪一行接着读），否则为空；与正文分开，正文被截断时它也还在
    more_note: str
    # 是否因输出上限没读完要求的行
    cut: bool


def _media_type(path: Path) -> str:
    """按扩展名定工作目录里文件的类型（share_file 与 read_file 共用）。认不出、或是压缩过的（x.tar.gz 会被认成
    tar 而丢掉 gzip）时，用 RFC 2046 的「未知二进制」类型。"""
    guessed, encoding = mimetypes.guess_type(path.name)
    return guessed if guessed and encoding is None else "application/octet-stream"


def _image_type(path: Path) -> str | None:
    """能交给模型看的图片的类型；其余文件当文本读。"""
    media_type = _media_type(path)
    return media_type if media_type in INLINE_IMAGE_TYPES else None


def _read_image(target: Path, media_type: str) -> list[str | BinaryContent]:
    """图片以内容交回；attachments.ToolImagesToFiles 把它存进文件存储，历史里只留引用，发请求时再展开给模型。"""
    if not target.is_file():
        raise NotFound(f"file not found: {target}")
    data = target.read_bytes()
    if len(data) > _MAX_IMAGE_BYTES:
        raise InvalidInput(
            f"image too large to view: {target} is {len(data)} bytes, the limit is {_MAX_IMAGE_BYTES} bytes; "
            "make a smaller copy first (e.g. `sips -Z 1568 big.png --out small.png`) and read that"
        )
    return [f"{target} ({media_type}, {len(data)} bytes)", BinaryContent(data, media_type=media_type)]


def _read_parts(scope: RunScope, reads: tuple[FileRange, ...], max_chars: int) -> str:
    """read_file 的多段读取：按次序读，共用一个输出预算。预算用完的那段在整行处停下并写明续读的 offset；之后
    的段不再读，最后一行逐个列出（这一行不参与截断），模型照着另起一次调用、不必猜。"""
    sections: list[tuple[FileRange, str]] = []
    skipped: list[FileRange] = []
    budget = max_chars
    for part in reads:
        target = resolve(scope, part.path)
        body_budget = budget - len(str(target)) - _PART_OVERHEAD
        if skipped or body_budget <= 0:
            skipped.append(part)
            continue
        try:
            if _image_type(target):
                raise InvalidInput("an image file cannot be read in reads; read it alone with path")
            excerpt = _read_range(target, part.offset, part.limit, body_budget)
        except (InvalidInput, NotFound, OSError) as e:
            # 标题用模型给的路径认出是哪一段（有的错误信息里不带路径）
            section = f"==> {part.path}: {e} <=="
        else:
            section = f"==> {target} {excerpt.span} <==\n{excerpt.body}{excerpt.more_note}"
            if excerpt.cut:
                budget = 0
        sections.append((part, section))
        budget -= len(section) + 2
    while True:
        body = "\n\n".join(s for _, s in sections)
        tail = _not_read_note(skipped)
        if len(body) + len(tail) <= max_chars or len(sections) <= 1:
            break
        # 放不下（未读列表太长，或某段第一行就超预算——_read_range 至少给一行）：把最后读的一段挤进列表，
        # 不在正文中间截
        part, _ = sections.pop()
        skipped.insert(0, part)
    # 兜底：只有第一段的第一行就比预算还长时才会截到正文
    return (truncate(body, max(max_chars - len(tail), 0)) + tail).lstrip("\n")


def _not_read_note(skipped: list[FileRange]) -> str:
    if not skipped:
        return ""
    listed = "; ".join(
        f"{p.path} offset={p.offset}" + (f" limit={p.limit}" if p.limit != _DEFAULT_READ_LIMIT else "")
        for p in skipped
    )
    return f"\n\n==> not read, output limit reached; read in another call: {listed} <=="


def _read_range(target: Path, offset: int, limit: int, budget: int) -> _Excerpt:
    """读 lines[offset:offset+limit]，带行号的正文不超过 budget 个字符：在最后一整行停下（至少给一行），并在
    more_note 里写明读到第几行、用什么 offset 接着读。"""
    if offset < 0 or limit < 1:
        # 负数在切片里有别的意思（从末尾数），会返回看起来正常、其实不对的内容
        raise InvalidInput(f"offset must be >= 0 and limit >= 1, got offset={offset} limit={limit}")
    if not target.is_file():
        raise NotFound(f"file not found: {target}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    if offset and offset >= len(lines):
        raise InvalidInput(f"offset {offset} is past the end: {target} has {len(lines)} lines")
    end = min(offset + limit, len(lines))
    rows: list[str] = []
    used = 0
    for i in range(offset, end):
        row = numbered(lines, i, i + 1)
        used += len(row) + (1 if rows else 0)
        if rows and used > budget:
            break
        rows.append(row)
    stop = offset + len(rows)
    if stop < end:
        # 给了 limit 且没读到文件末尾时才提 limit；默认 limit 的窗口不是模型要的，照着读会以为文件到那里就完了
        explicit = limit != _DEFAULT_READ_LIMIT and end < len(lines)
        rest = f", limit={end - stop}" if explicit else ""
        note = (
            f"\n… output limit reached: shown up to line {stop} of {len(lines)}; "
            f"continue with offset={stop}{rest}"
        )
    else:
        more = len(lines) - end
        note = f"\n… {more} more lines" if more > 0 else ""
    return _Excerpt(
        span=f"lines {offset + 1}-{stop} of {len(lines)}" if lines else "(empty file)",
        body="\n".join(rows),
        more_note=note,
        cut=stop < end,
    )


def _edit_target(scope: RunScope, path: str | None, e: TextEdit, i: int) -> Path:
    """一处修改要改的文件：它自己的 path，没有就用顶层的。"""
    chosen = e.path if e.path is not None else path
    if chosen is None:
        raise InvalidInput(f"edit {i} has no path: give it its own path or pass the top-level path")
    return resolve(scope, chosen)


def _edit_report(target: Path, before: str, content: str, count: int) -> str:
    """一个文件改完后的回显：替换数、行数和改动处的行（带上下文与行号），省掉回读确认。"""
    lines = content.splitlines()
    head = f"edited {target} ({count} replacement{'s' if count > 1 else ''}; now {len(lines)} lines)"
    ranges = text_edit.changed_ranges(before, content, _EDIT_CONTEXT_LINES)
    if not ranges:
        # 没有可展示的行：没变、删空了，或只改了行尾（换行符、末尾换行）
        if before == content:
            return f"{head}; the text is unchanged"
        return f"{head}; the file is now empty" if not lines else f"{head}; only line endings changed"
    excerpts = [f"lines {start + 1}-{end}:\n{numbered(lines, start, end)}" for start, end in ranges]
    return "\n".join([f"{head}. Changed lines with context:", *excerpts])


@dataclass(frozen=True)
class _Located:
    """模型给的一处修改：应用时按它的定位方式换成对应的 Edit，定位参数不对也在这时报错，由 apply_each 统一标出是第几处。"""

    target: Path
    edit: TextEdit

    def apply(self, current: str, what: str) -> tuple[str, int]:
        return _change_of(self.target, self.edit).apply(current, what)


def _change_of(target: Path, e: TextEdit) -> text_edit.Edit:
    """一处修改按它给的定位方式（恰好一种）换成对应的 Edit。"""
    lines = (e.start_line, e.end_line, e.anchor)
    if any(v is not None for v in lines) and None in lines:
        raise InvalidInput("start_line, end_line and anchor go together: give all three")
    given = [e.old_text is not None, e.start_line is not None, e.pattern is not None]
    if sum(given) != 1:
        raise InvalidInput(
            "locate the text in exactly one way: old_text, start_line/end_line/anchor, or pattern"
        )
    if e.old_text is not None:
        return text_edit.Replace(e.old_text, e.new_text, e.replace_all)
    if e.pattern is not None:
        return PatternEdit(e.pattern, language_of(target), e.new_text, e.replace_all)
    if e.replace_all:
        raise InvalidInput("replace_all does not apply to a line range")
    assert e.start_line is not None and e.end_line is not None and e.anchor is not None
    return text_edit.LineRange(e.start_line, e.end_line, e.anchor, e.new_text)
