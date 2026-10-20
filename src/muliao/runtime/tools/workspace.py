"""文件与 shell 工具。相对路径以 agent 工作目录为基准；不做强隔离（单人本机使用，见架构文档）。"""

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import FunctionToolset, RunContext, ToolReturn
from pydantic_ai.tools import ToolDefinition

from muliao.conventions import TOOL_ATTACHMENTS, attachment_of
from muliao.errors import InvalidInput, NotFound
from muliao.files import FileStore
from muliao.models import JobStatus
from muliao.runtime.jobs import JobRunner
from muliao.runtime.scope import RunScope
from muliao.runtime.shell import run_bash
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.transcript import truncate
from muliao.vault import Vault

log = logging.getLogger(__name__)

_LIST_LIMIT = 500


@dataclass(frozen=True)
class ShellLimits:
    default_timeout: float
    max_timeout: float
    max_output_chars: int


def resolve(scope: RunScope, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else scope.workspace / p


async def _only_with_a_session(ctx: RunContext[RunScope], tool: ToolDefinition) -> ToolDefinition | None:
    """后台任务属于会话：没有会话的运行（临时 agent、反思）开不了也看不到它们。"""
    return tool if ctx.deps.session_id is not None else None


def workspace_toolset(
    vault: Vault, limits: ShellLimits, files: FileStore, jobs: JobRunner
) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def read_file(ctx: RunContext[RunScope], path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a text file. Relative paths are resolved against your workspace.

        Args:
            path: File path.
            offset: First line to return (0-based).
            limit: Maximum number of lines to return.
        """
        if offset < 0 or limit < 1:
            # 负数在切片里有别的意思（从末尾数），会返回看起来正常、其实不对的内容
            raise InvalidInput(f"offset must be >= 0 and limit >= 1, got offset={offset} limit={limit}")
        target = resolve(ctx.deps, path)
        if not target.is_file():
            raise NotFound(f"file not found: {target}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset : offset + limit]
        body = "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(selected))
        more = len(lines) - offset - len(selected)
        return truncate(body, limits.max_output_chars) + (f"\n… {more} more lines" if more > 0 else "")

    @ts.tool
    async def write_file(ctx: RunContext[RunScope], path: str, content: str) -> str:
        """Create or overwrite a text file. Parent directories are created as needed.

        Args:
            path: File path.
            content: Full file content.
        """
        target = resolve(ctx.deps, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {target}"

    @ts.tool
    async def edit_file(ctx: RunContext[RunScope], path: str, old_text: str, new_text: str) -> str:
        """Replace text in a file. `old_text` must occur exactly once.

        Args:
            path: File path.
            old_text: Exact text to replace.
            new_text: Replacement text.
        """
        target = resolve(ctx.deps, path)
        if not target.is_file():
            raise NotFound(f"file not found: {target}")
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text) if old_text else 0
        if count != 1:
            raise InvalidInput(f"old_text must occur exactly once in {target}, found {count}")
        target.write_text(content.replace(old_text, new_text), encoding="utf-8")
        return f"edited {target}"

    @ts.tool
    async def list_files(ctx: RunContext[RunScope], path: str = ".", recursive: bool = False) -> str:
        """List directory entries. Directories end with '/'.

        Args:
            path: Directory path.
            recursive: List all descendants instead of direct children.
        """
        target = resolve(ctx.deps, path)
        if not target.is_dir():
            raise NotFound(f"directory not found: {target}")
        entries = sorted(target.rglob("*") if recursive else target.iterdir())
        lines = [f"{e.relative_to(target)}{'/' if e.is_dir() else ''}" for e in entries[:_LIST_LIMIT]]
        if len(entries) > _LIST_LIMIT:
            lines.append(f"… {len(entries) - _LIST_LIMIT} more entries")
        return "\n".join(lines) or "(empty)"

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

    # 时限是配置的值，写进说明：模型不知道默认时限，长命令会被杀了才学会加 timeout
    @ts.tool(
        description="Run a shell command (bash) in your workspace and return its exit code and combined output. "
        f"Commands are killed after {limits.default_timeout:g} seconds unless you pass a longer `timeout` "
        f"(at most {limits.max_timeout:g}). For anything longer, or to keep working while it runs, pass "
        "`background: true`: it returns a job id at once and has no time limit; its output goes to a log file in "
        "your workspace, you get an event when it finishes and a check-in event about every "
        f"{jobs.check_seconds:g} seconds while it runs (decide then whether to keep waiting or call `job_kill`)."
    )
    async def run_shell(
        ctx: RunContext[RunScope],
        command: str,
        timeout: float | None = None,  # noqa: ASYNC109  工具参数，面向模型的命名
        # 不写成 `list[str] | None`：可为空的数组在 schema 里是 anyOf[array, null]，模型更容易把数组写成 JSON 字符串
        secrets: tuple[str, ...] = (),
        background: bool = False,
    ) -> str:
        """The description is set in the decorator (it carries the configured time limits).

        Args:
            command: The command line to run.
            timeout: Seconds before the command is killed (foreground only).
            secrets: Vault keys (your private ones or public ones) to expose to the command as environment
                variables of the same name.
                Secret values in the output are replaced by `[secret:KEY]`.
            background: Run it as a background job instead of waiting for it.
        """
        if background:
            if timeout is not None:
                raise InvalidInput(
                    "background jobs have no timeout; leave `timeout` out (kill them with job_kill)"
                )
            job = await jobs.start(
                session_id=ctx.deps.session_id,
                agent_id=ctx.deps.agent_id,
                command=command,
                secrets=list(secrets),
                reply_to=ctx.deps.reply_to,
            )
            return (
                f"started background job {job.id}; output goes to {job.log_path}. "
                "You will get an event when it finishes; keep working or end your turn."
            )
        timeout = limits.default_timeout if timeout is None else timeout
        if not 0 < timeout <= limits.max_timeout:
            raise InvalidInput(f"timeout must be in (0, {limits.max_timeout}] seconds")
        secret_values = await vault.resolve_many(ctx.deps.agent_id, list(secrets))
        result = await run_bash(
            command, cwd=ctx.deps.workspace, secrets=secret_values, timeout=timeout, merge_stderr=True
        )
        status = (
            f"exit_code: {result.exit_code}"
            if result.exit_code is not None
            else f"killed after {timeout}s timeout"
        )
        text = result.stdout
        return f"{status}\n{truncate(text, limits.max_output_chars)}"

    @ts.tool(prepare=_only_with_a_session)
    async def job_status(ctx: RunContext[RunScope], job_id: int) -> str:
        """Show a background job's status and the last lines of its output (read its log file for all of it).

        Args:
            job_id: The id `run_shell(background: true)` returned.
        """
        info = await jobs.info(job_id, ctx.deps.agent_id)
        state = f"exited with code {info.exit_code}" if info.status == JobStatus.EXITED else info.status.value
        return (
            f"job {info.id}: {state} ({info.running_for})\ncommand: {info.command}\nlog: {info.log_path}\n"
            f"last lines:\n{info.tail or '(no output yet)'}"
        )

    @ts.tool(prepare=_only_with_a_session)
    async def job_kill(ctx: RunContext[RunScope], job_id: int) -> str:
        """Kill a running background job (its whole process tree). Its final status arrives as an event.

        Args:
            job_id: The id `run_shell(background: true)` returned.
        """
        await jobs.kill(job_id, ctx.deps.agent_id)
        return f"kill signal sent to job {job_id}; its final status will arrive as an event"

    return ts
