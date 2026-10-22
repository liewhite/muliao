"""文件与 shell 工具。相对路径以 agent 工作目录为基准；不做强隔离（单人本机使用，见架构文档）。

workspace_toolset 是文件与前台 shell（临时 agent）；有会话的运行用 jobs.tools.session_workspace_toolset，它由这里的
文件工具与前台执行加上后台任务组成。
"""

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import FunctionToolset, RunContext, ToolReturn

from muliao.agent_runtime.conventions import TOOL_ATTACHMENTS, attachment_of
from muliao.errors import InvalidInput, NotFound
from muliao.files.api import FileStore
from muliao.platform import text_edit
from muliao.platform.shell import run_bash
from muliao.platform.text_edit import truncate
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.vault.api import Vault

log = logging.getLogger(__name__)

_MAX_LINE_CHARS = 2000
# edit_file 成功后展示的改动前后上下文行数：够确认改对了，不必再回读文件
_EDIT_CONTEXT_LINES = 2
_DEFAULT_READ_LIMIT = 2000


class TextEdit(BaseModel):
    # edit_file 一次调用里的一处替换（类 docstring 会进 schema 给模型看，所以用注释）
    model_config = ConfigDict(extra="forbid")

    old_text: str = Field(description="Exact text to replace.")
    new_text: str = Field(description="Replacement text.")
    replace_all: bool = Field(default=False, description="Replace every occurrence instead of exactly one.")


class FileRange(BaseModel):
    # read_file 一次调用里要读的一段
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="File path.")
    offset: int = Field(default=0, description="First line to return (0-based).")
    limit: int = Field(default=_DEFAULT_READ_LIMIT, description="Maximum number of lines to return.")


@dataclass(frozen=True)
class ShellLimits:
    default_timeout: float
    max_timeout: float
    max_output_chars: int


def resolve(scope: RunScope, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else scope.workspace / p


_SHELL_DESCRIPTION = (
    "Run a shell command (bash) in your workspace and return its exit code and combined output. "
    "Commands are killed after {default:g} seconds unless you pass a longer `timeout` (at most {max:g}). "
    "Only the first {max_output} characters of output are kept, so limit it yourself: pipe through `| head` or `| tail`, "
    "use `rg -m N`, `cut -c1-200`, and read only the part of a long log you need. "
    "Search with ripgrep, not grep/find: `rg -n PATTERN [PATH]` for contents (`-g '*.py'` filters files, `-C 3` "
    "adds context), `rg --files [-g GLOB]` to find files; it skips .git and ignored files such as node_modules. "
    "Read files with read_file (one call can read several files or parts, with line numbers) and change them "
    "with edit_file / write_file rather than cat, sed or heredocs."
)


def shell_description(limits: ShellLimits) -> str:
    """时限与输出上限是配置的值，写进说明：模型不知道它们，长命令被杀、长输出被截了才学会。"""
    return _SHELL_DESCRIPTION.format(
        default=limits.default_timeout, max=limits.max_timeout, max_output=limits.max_output_chars
    )


def workspace_toolset(vault: Vault, limits: ShellLimits, files: FileStore) -> FunctionToolset[RunScope]:
    """文件与前台 shell。"""
    ts = file_toolset(limits, files)

    @ts.tool(description=shell_description(limits))
    async def run_shell(
        ctx: RunContext[RunScope],
        command: str,
        timeout: float | None = None,  # noqa: ASYNC109  工具参数，面向模型的命名
        # 不写成 `list[str] | None`：可为空的数组在 schema 里是 anyOf[array, null]，模型更容易把数组写成 JSON 字符串
        secrets: tuple[str, ...] = (),
    ) -> str:
        """The description is set in the decorator (it carries the configured limits).

        Args:
            command: The command line to run.
            timeout: Seconds before the command is killed.
            secrets: Vault keys (your private ones or public ones) to expose to the command as environment
                variables of the same name.
                Secret values in the output are replaced by `[secret:KEY]`.
        """
        return await run_foreground(vault, limits, ctx.deps, command, timeout, secrets)

    return ts


async def run_foreground(
    vault: Vault,
    limits: ShellLimits,
    scope: RunScope,
    command: str,
    timeout: float | None,  # noqa: ASYNC109  run_shell 的参数原样传入
    secrets: tuple[str, ...],
) -> str:
    timeout = limits.default_timeout if timeout is None else timeout
    if not 0 < timeout <= limits.max_timeout:
        raise InvalidInput(f"timeout must be in (0, {limits.max_timeout}] seconds")
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
    ) -> str:
        """Read a text file; lines are numbered. Relative paths are resolved against your workspace.
        To read several files or several parts of one file in one call, pass `reads` instead of
        `path`/`offset`/`limit`; each part is shown under a `==> path lines A-B of N <==` header.

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
            excerpt = _read_range(target, offset, limit)
            return truncate(excerpt.body, limits.max_output_chars) + excerpt.more_note
        sections = []
        for part in reads:
            target = resolve(ctx.deps, part.path)
            try:
                excerpt = _read_range(target, part.offset, part.limit)
            except (InvalidInput, NotFound, OSError) as e:
                # 标题用模型给的路径认出是哪一段（有的错误信息里不带路径）
                sections.append(f"==> {part.path}: {e} <==")
                continue
            sections.append(f"==> {target} {excerpt.span} <==\n{excerpt.body}{excerpt.more_note}")
        return truncate("\n\n".join(sections), limits.max_output_chars)

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
        path: str,
        old_text: str | None = None,
        new_text: str | None = None,
        replace_all: bool = False,
        edits: tuple[TextEdit, ...] = (),
    ) -> str:
        """Replace text in a file. `old_text` must occur exactly once, unless `replace_all` is set.
        Copy `old_text` exactly from the file as it is now (without read_file's line-number prefix), with enough
        surrounding lines to be unique. To change several places in the same file in one call, pass `edits`
        instead of `old_text`/`new_text`: they are applied in order, and if any of them fails nothing is written.

        Args:
            path: File path.
            old_text: Exact text to replace (omit when using `edits`).
            new_text: Replacement text; empty deletes `old_text` (omit when using `edits`).
            replace_all: Replace every occurrence (e.g. renaming a variable) instead of exactly one (omit when
                using `edits`).
            edits: List of {old_text, new_text, replace_all} replacements in this file, applied in order (each
                sees the result of the earlier ones); each old_text must occur exactly once unless its
                replace_all is set.
        """
        if edits and (old_text is not None or new_text is not None or replace_all):
            raise InvalidInput("pass either old_text/new_text/replace_all or edits, not both")
        if not edits and (old_text is None or new_text is None):
            raise InvalidInput("old_text and new_text are required (or pass edits)")
        target = resolve(ctx.deps, path)
        if not target.is_file():
            raise NotFound(f"file not found: {target}")
        changes = [(e.old_text, e.new_text, e.replace_all) for e in edits] or [
            (old_text, new_text, replace_all)
        ]
        before = target.read_text(encoding="utf-8")
        content, count = text_edit.replace_each(before, changes, str(target))
        target.write_text(content, encoding="utf-8")
        lines = content.splitlines()
        head = f"edited {target} ({count} replacement{'s' if count > 1 else ''}; now {len(lines)} lines)"
        ranges = text_edit.changed_ranges(before, content, _EDIT_CONTEXT_LINES)
        if not ranges:
            # 没有可展示的行：没变、删空了，或只改了行尾（换行符、末尾换行）
            if before == content:
                return f"{head}; the text is unchanged"
            return f"{head}; the file is now empty" if not lines else f"{head}; only line endings changed"
        excerpts = [f"lines {start + 1}-{end}:\n{_numbered(lines, start, end)}" for start, end in ranges]
        return truncate(
            "\n".join([f"{head}. Changed lines with context:", *excerpts]), limits.max_output_chars
        )

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
        # 扩展名认不出、或是压缩过的（x.tar.gz 会被认成 tar 而丢掉 gzip）时，用 RFC 2046 的「未知二进制」类型，按附件下载
        guessed, encoding = mimetypes.guess_type(target.name)
        media_type = guessed if guessed and encoding is None else "application/octet-stream"
        meta = await files.put_path(target.name, media_type, target)
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
    # 后面还有行时注明还剩多少，否则为空；与正文分开，正文被截断时它也还在
    more_note: str


def _read_range(target: Path, offset: int, limit: int) -> _Excerpt:
    if offset < 0 or limit < 1:
        # 负数在切片里有别的意思（从末尾数），会返回看起来正常、其实不对的内容
        raise InvalidInput(f"offset must be >= 0 and limit >= 1, got offset={offset} limit={limit}")
    if not target.is_file():
        raise NotFound(f"file not found: {target}")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    if offset and offset >= len(lines):
        raise InvalidInput(f"offset {offset} is past the end: {target} has {len(lines)} lines")
    end = min(offset + limit, len(lines))
    more = len(lines) - end
    return _Excerpt(
        span=f"lines {offset + 1}-{end} of {len(lines)}" if lines else "(empty file)",
        body=_numbered(lines, offset, end),
        more_note=f"\n… {more} more lines" if more > 0 else "",
    )


def _numbered(lines: list[str], start: int, end: int) -> str:
    """lines[start:end]，每行前加 1 起算的行号和制表符（read_file 与 edit_file 的回显同一格式）。"""
    # 一行压缩过的 JS/JSON 能吃掉整个输出预算，单行截断
    return "\n".join(f"{i + 1}\t{_clip(lines[i])}" for i in range(start, end))


def _clip(line: str) -> str:
    return line if len(line) <= _MAX_LINE_CHARS else f"{line[:_MAX_LINE_CHARS]}…[line truncated]"
